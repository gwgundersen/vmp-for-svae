#!/bin/bash

#SBATCH --mem 100G
#SBATCH -N 1
#SBATCH --ntasks-per-node=4
#SBATCH --ntasks-per-socket=2
#SBATCH --gres=gpu:1
#SBATCH -o /scratch/gpfs/gwg3/vmp-for-svae/out.txt
#SBATCH -t 5:00:00
#SBATCH --mail-user=ggundersen@princeton.edu

module load cudatoolkit/8.0 cudann/cuda-8.0/5.1
module load anaconda3
source activate san-cpu-env

cd /scratch/gpfs/gwg3/vmp-for-svae

python experiments.py
