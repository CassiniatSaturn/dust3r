#!/bin/bash
source /home/xizh00005/.bashrc
conda activate torch_env
ROOT='/home/xizh00005/project/dust3r'
cd $ROOT
python inference.py -item "box" -num_view 2
