#!/usr/bin/python3
import argparse
import tempfile
import yaml
import json
import subprocess
import base64
import re
import sys
import time
import shlex
import copy
import os
import signal
from kabuki import basic_run
from kabuki.query_machine_info import get_full_command, parse_full_output

my_folder = os.path.dirname(os.path.realpath(__file__))

def run_all(commands):
    procs = []
    for command in commands:
        proc = subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        procs.append(proc)
    outputs = []
    for proc in procs:
        out, err = proc.communicate()
        print(err,file=sys.stderr)
        if proc.returncode != 0:
            print(out,file=sys.stderr)
            out = None
        else:
            out = out.decode("utf-8")
        outputs.append(out)
    return outputs

def find_all_machine_info(machines):
    cmd = get_full_command()
    commands = [basic_run.make_ssh_command(mac, cmd) for mac in machines]
    outputs = run_all(commands)
    if not all(outputs):
        fail_machines = [(mach, " ".join(cmd)) for out,mach,cmd in zip(outputs, machines, commands) if out is None]
        raise RuntimeError("could not connect to machines: "+json.dumps(fail_machines))
    parsed_outs = [parse_full_output(out) for out in outputs]
    return parsed_outs

def machine_limit_over(machine_limit):
    return (machine_limit['reserved'] > 1 or
        machine_limit['cpu_count'] < -2 or
        machine_limit['mem_free'] < 0 or
        any(gpu['free'] < 0 for gpu in machine_limit['gpus']) or
        any(gpu['utilization'] > 1.2 for gpu in machine_limit['gpus']) or
        any(gpu['reserved'] > 1 for gpu in machine_limit['gpus']))

def subtract_process_req(machine_limit, args):
    if args.reserve:
        machine_limit['reserved'] += 1
    machine_limit['cpu_count'] -= args.num_cpus
    machine_limit['mem_free'] -= args.memory_required
    gpu_idx = 0
    if not args.no_gpu_required:
        gpu_choice = None
        for i,gpu in enumerate(machine_limit['gpus']):
            if gpu_choice is None or gpu_choice['reserved'] or gpu_choice['free'] - args.gpu_memory_required <= 0 or gpu_choice['utilization'] > gpu['utilization']:
                gpu_choice = gpu
                gpu_idx = i
        gpu_choice['free'] -= args.gpu_memory_required
        gpu_choice['utilization'] += args.gpu_utilization
        if not args.no_reserve_gpu:
            gpu_choice['reserved'] += 1
    return gpu_idx

def init_machine_limit(machine_limit):
    machine_limit['reserved'] = 0
    machine_limit['cpu_count'] *= (1-machine_limit['cpu_usage'])
    for gpu in machine_limit['gpus']:
        gpu['reserved'] = 0

def get_process_limit(machine_limit, args):
    '''
    machine limit looks like this:
    {"cpu_usage": 0.124, "mem_free": 30607, "cpu_count": 24, "gpus": [{"name": "GeForce RTX 2060", "mem": 5934, "free": 5933, "utilization": 0.0}, {"name": "GeForce RTX 2060", "mem": 5932, "free": 5931, "utilization": 0.0}]}
    '''
    machine_limit = copy.deepcopy(machine_limit)

    if not machine_limit['gpus'] and not args.no_gpu_required:
        return []

    init_machine_limit(machine_limit)
    gpu_choices = []
    while True:
        gpu_choice = subtract_process_req(machine_limit, args)
        if machine_limit_over(machine_limit):
            break
        gpu_choices.append(gpu_choice)
    return gpu_choices

def make_basic_run_command(machine, job_name, export_prefix, command, gpu_choice, args):
    basic_cmd = f"python -u {os.path.join(my_folder,'basic_run.py')} --copy-forward {' '.join(args.copy_forward)}  --copy-backwards {' '.join(args.copy_backwards)} --machine={machine} --job-name={job_name} {'--verbose' if args.verbose else ''}".split()
    cmd = basic_cmd + [export_prefix+" "+command]
    return cmd

def make_kabuki_run_command(machine, job_name, export_prefix, command, gpu_choice, args):
    final_command = command
    if "--copy-forward" not in command:
        final_command += f" --copy-forward {' '.join(args.copy_forward)} "
    if "--copy-backwards" not in command:
        final_command += f" --copy-backwards {' '.join(args.copy_backwards)} "
    if "--job-name" not in command:
        final_command += f" --job-name {job_name} "
    if args.verbose:
        final_command += f" --verbose "
    final_command += f" --machine {machine} "

    split_cmd = shlex.split(final_command)[1:]
    parse_results = basic_run.parse_args(split_cmd)
    # final_command = final_command.replace(parse_results.command, f" {export_prefix} {parse_results.command} ")
    # catted_cmd =  f" {export_prefix} {parse_results.command} "
    resulting_command = make_basic_run_command(machine, parse_results.job_name, export_prefix, parse_results.command, gpu_choice, parse_results)

    return resulting_command, parse_results.job_name

def main():
    parser = argparse.ArgumentParser(
        description='Run a batched command',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('--copy-forward', nargs='*', default=[], help='Files and folders to copy when running the command. Defaults to everything in the current working directory')
    parser.add_argument('--copy-backwards', nargs='*', default=[], help='Files and folders to copy back from the worker running the command. Defaults to everything in the current working directory')
    parser.add_argument('--machines', nargs='*', help='machine id', required=True)
    parser.add_argument('--num-cpus', type=int, default=1, help='cpus to reserve for the job')
    parser.add_argument('--memory-required', type=int, default=7000, help='memory to reserve for the job')
    parser.add_argument('--reserve', action="store_true", help='reserve entire machine for job')
    parser.add_argument('--no-reserve-gpu', action="store_true", help='reserve entire machine for job')
    parser.add_argument('--no-gpu-required', action="store_true", help='is a gpu required for the job')
    parser.add_argument('--gpu-memory-required', type=int, default=1000, help='gpu memory to reserve for the job')
    parser.add_argument('--gpu-utilization', type=float, default=0.75, help='gpu utilization consumed')
    parser.add_argument('--verbose', action="store_true", help='print out debug information')
    parser.add_argument('--dry-run', action="store_true", help='just print out first round of commands')
    parser.add_argument('--kabuki-commands', action="store_true", help='Whether the batch file should be interpreted as kabuki commands instead of bash commands')
    parser.add_argument('filename', help="a file where each line contains a command")

    args = parser.parse_args()

    lines = open(args.filename).readlines()
    machine_configs = [basic_run.load_data_from_yaml(mac) for mac in args.machines]
    machine_infos = find_all_machine_info(machine_configs)
    machine_gpu_choices = [get_process_limit(info, args) for info in machine_infos]
    machine_proc_limits = [len(c) for c in machine_gpu_choices]
    print("machine limits: ", {name:limit for name, limit in zip(args.machines,machine_proc_limits)})
    print("machine gpu choices:",machine_gpu_choices)
    machine_procs = [[None for i in range(limit)] for limit in machine_proc_limits]
    save_filename = args.filename.replace("/","_")
    job_names = [f"{save_filename}.{line_num+1}" for line_num in range(len(lines))]
    for line_num in range(len(lines)):
        if os.path.exists(f"./job_results/{job_names[line_num]}"):
            print(f"WARNING: job results already exists for line {line_num+1}, skipping evaluation: delete if you wish to rerun")

    os.makedirs("./job_results/",exist_ok=True)
    line_num = 0
    try:
        all_done = False
        while not all_done:
            all_done = True
            waiting_only = True
            for mac,gpu_choices,procs in zip(args.machines, machine_gpu_choices, machine_procs):
                for i,(gpu_choice, proc) in enumerate(zip(gpu_choices, procs)):
                    if proc is not None and proc[1].poll() is not None:
                        message = "finished" if proc[1].returncode == 0 else "failed"
                        finished_num = proc[0]
                        job_name = job_names[finished_num]
                        print(f"{message}: {job_name}; {lines[finished_num].strip()}",flush=True)
                        proc = procs[i] = None
                    if proc is None and line_num < len(lines):
                        export_prefix = f"export CUDA_VISIBLE_DEVICES={gpu_choice} &&" if not args.reserve and not args.no_gpu_required else ""
                        command = lines[line_num].strip()
                        job_name = job_names[line_num]

                        if args.kabuki_commands:
                            job_cmd, new_job_name = make_kabuki_run_command(mac, job_name, export_prefix, command, gpu_choice, args)
                            job_name = job_names[line_num] = new_job_name
                        else:
                            job_cmd = make_basic_run_command(mac, job_name, export_prefix, command, gpu_choice, args)
                        print(job_name)
                        if os.path.exists(f"./job_results/{job_name}"):
                            print("skipping", command,flush=True)
                        else:
                            if args.verbose or args.dry_run:
                                fancy_job_command = ' '.join(job_cmd)
                                print(fancy_job_command)
                            if not args.dry_run:
                                print(f"started: {job_name};  {command}",flush=True)
                                stdout_file = open(f"./job_results/{job_name}.out",'a',buffering=1)
                                stderr_file = open(f"./job_results/{job_name}.err",'a',buffering=1)
                                process = subprocess.Popen(job_cmd,stdout=stdout_file, stderr=stderr_file)#,creationflags=subprocess.DETACHED_PROCESS)
                                time.sleep(0.2)
                                proc = procs[i] = (line_num, process)
                        line_num += 1
                        all_done = False
                        waiting_only = False

                    if proc is not None:
                        all_done = False
            if waiting_only:
                time.sleep(1)
    except BaseException as be:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        print("interrupting tasks")
        for procs in machine_procs:
            for proc in procs:
                if proc is not None:
                    proc[1].send_signal(signal.SIGINT)
        print("waiting for tasks to terminate")
        for procs in machine_procs:
            for proc in procs:
                if proc is not None:
                    proc[1].wait()
        raise be


if __name__ == "__main__":
    main()
