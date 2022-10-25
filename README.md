# `ssh` Scheduler

A minimal-setup ssh based distributed batch processing system for ML.

Intended use case is for a researcher running many experiments on many different machines, but without the headache of setting up and using SLURM or Kubernettes, and without the stress and error of sshing into each machine and running each job by hand.

## Install

```
pip install git+https://github.com/benblack769/ssh-scheduler.git
```

## Features

* **Minimal install on remote:** Only requires passwordless ssh login.
* **Minimal local configuration:** Only requires a 4 line config for each machine (example below)
* **Minimal job configuration:** A job is just a bash script with one command per line. To specify resource usage for each job, just pass in hardware resource requirements as command line arguments
* **Automatic job allocation on heterogenous hardware:** System automatically determines hardware resources and adjusts number of jobs per system
* **Automatic GPU allocation:** Do you want multiple jobs per machine on different GPUs? Multiple jobs on a single GPU? Both options are easily specified by command line arguments.
* **Non exclusive use of machines:** Free resources on machines are calculated at batch submission. The system will only use free resources on remote machines (calculated at batch submission time), meaning that other jobs //users can be running on those systems without resource overload issues.
* **Low latency job submission:** Jobs are fast to start, a whole batch can be started and finished in 5-10 seconds, under optimal conditions.

## Tools

Ssh Scheduler has several powerful command line tools to execute remote tasks.

* execute_remote: a CLI for executing a command on a remote machine
* execute_batch: Distribute many commands across many remote machines, with specified resource constraints. Queries remote machines for resource capacity. Full support for Nvidia GPU resource distribution.
* execute_to: a CLI for executing a single command on many remote machines (typically for installation or other system management)
* combine_folders: a CLI for combining a set of folders data. Meant to collate output of batch of commands.

## Modules

* execute remote workflow
* query remote machine info
* machine virtual resources handler (performs allocation validation/scoring as well as allocation and deallocation resource limits)
    * __init__(avaliable_resources)
    * score(allocation) # returns -inf if not enough resoures
    * allocate(allocation) # throws error if not enough resources, updates object state
* machine runner: sends signals on start and stop of queued job. Puts time in-between queue and start to manage load on remote server.
    * __init__(machine_info)
    * queue_up(command, forward_loc, backward_loc, job_name, etc)
    * register_started(callback(job_name))
    * register_ended(callback(job_name))
* combine_folders:
    * Identical files from different folders are collapsed to the same files, different paths are all included, same path with different filename results in error
    * useage: combine_folders dest src1 src2 ...

## Example

Full example located in `example` folder. Need to change `example/machine.yaml` to get it to work.

### Step 1: Customize machine config(s)

To define a remote machine you need a yaml file like `example/machine.yaml` (displayed below):

```
username: ben       # Replace with your username on the machine
ip: 105.29.123.241  # Replace with your machine's url or IP address
port: 22    # leave at default value of 22 unless you know what you are doing
# replace with a path to an ssh key to the machine (MUST BE PASSWORDLESS SSH KEY)
# for a guide on how to set up passwordless ssh see a guide such as
# https://www.redhat.com/sysadmin/passwordless-ssh
ssh_key_path: ~/.ssh/id_ssh_key
```

The system looks in `~/.local/var/` and in your local directory for the machine config

### Step 2: Define job

To define a job, you need a batch script where every line is a command you want to run on a different machine. For example `example/batch_script.sh` shown below:

```
echo "one job"
echo "another job"
echo "another job" && echo "a combined job (bash syntax applies)"
bash examples/local.sh && echo "you can also call local files because they are copied by the remote by the default value of --copy-forwards"
```

If you want, you can specify the forward and backward copies at the job level instead of globally. You can do that by running partially complete `execute_remote` commands like so:

```
execute_remote --copy-forwards example1 test.py --copy-backwards example1 --job-name example1 'python test.py example1'
execute_remote --copy-forwards example2 test.py --copy-backwards example2 --job-name example2 'python test.py example2'
```

This will copy forward example1 to the remote machine for the example1 job and example2 for the example2 job. Machines and GPUs will be allocated automatically, so any specification of a machine will be ignored, and specifying a GPU may screw things up.

### Step 3: Figure out hardware requirements

There are three types of hardware requirements: reservations, memory and compute. All three are supported at both CPU and GPU levels.

* A reserved machine or reserved GPU can only be used by one process.
* Free memory on remote machine is fetched when batch script runs. The total requested memory of jobs allocated to the system is not allowed to exceed the free memory in that system
* Free CPU and GPU utilization is fetched when batch script runs. A small overload of utilization is tolerated, as systems often work quite well under overutilization due to efficient OS level resource allocation.


### Step 4: Run job

To run the job, run the `execute_batch` command. A minimal example is below (also in `example/run.sh`):

```
execute_batch example/batch_script.sh --machines example/machine.yaml example/machine2.yaml
```

Note that this command will give an error until you specify valid machines for `example/machine.yaml` and `example/machine2.yaml`

This example uses the default values for hardware allocation. To specify custom hardware requirements, you can do:

**No GPU**

```
execute_batch example/batch_script.sh --machines example/machine.yaml --no-gpu-required
```

**Change required memory**

Memory is measured in megabytes. Note that GPUs are reserved by default, must be turned off to have multiple jobs per GPU.

```
execute_batch example/batch_script.sh --machines example/machine.yaml \
    --memory-required=10000 --gpu-memory-required=3000 --no-reserve-gpu
```

**Change required utilization**

CPU utilization is measured in cores, GPU utilization in proportions.

```
execute_batch example/batch_script.sh --machines example/machine.yaml \
    --num-cpus=8 --gpu-utilization=0.5
```


**Reserve entire machine**

When entire machine is reserved or no GPU is requested, CUDA_VISIBLE_DEVICES will not be set.

```
execute_batch example/batch_script.sh --machines example/machine.yaml \
    --reserve
```

### Step 5: Monitor progress

Here is real output from the program: run on `execute_batch example/batch_script.sh --machines my_machine.yaml --memory-required=2000` Annotations for the readme are added in comments to the side

```
machine limits:  {'my_machine.yaml': 2}                     # it will allocate 2 jobs at a time on this machine
machine gpu choices: [[0, 1]]                               # it will allocate one job on cuda:0 and one on cuda:1
WARNING: job results already exists for line 3, skipping evaluation: delete if you wish to rerun    # An up front warning for tasks skipped due to existing results
started: example_batch_script.sh.1;  export CUDA_VISIBLE_DEVICES=0 && echo "one job"  # jobs started
started: example_batch_script.sh.2;  export CUDA_VISIBLE_DEVICES=1 && echo "jobs will tell you if they fail" && exit 1
finished: example_batch_script.sh.1; echo "one job"         # jobs finishing normally
started: example_batch_script.sh.3;  export CUDA_VISIBLE_DEVICES=0 && echo "another job" && echo "a combined job (bash syntax applies)"
failed: example_batch_script.sh.2; echo "jobs will tell you if they fail" && exit 1     # jobs exiting with non-zero code (indicating a possible error, should check job_results/example_batch_script.sh.2)
started: example_batch_script.sh.4;  export CUDA_VISIBLE_DEVICES=1 && bash local.sh && echo "you can also call local files because they are copied by the remote by the default value of --copy-forwards"
finished: example_batch_script.sh.3; echo "another job" && echo "a combined job (bash syntax applies)"
failed: example_batch_script.sh.4; bash local.sh && echo "you can also call local files because they are copied by the remote by the default value of --copy-forwards" # in this case the error is that local.sh is not in the current directory (need to run examples/local.sh)
```

### Step 6: Get results

Results are copied to the `job_results` directory in your current folder. Results will not be replaced (we don't want to delete your calculations), instead a job will be skipped if the data is already there, showing a warning in the console. Be prepared to remove data if jobs crash.

Stdout and stderr of the program is put in `job_results/<job_name>.out` and `job_results/<job_name>.err`, respectively.

Files specified by `--copy-backwards` (by default the current working directory) are placed in the `job_results/<job_name>/ `
