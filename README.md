# Graph Explicit Pooling for Graph-Level Representation Learning
Implementation for paper: Graph Explicit Pooling for Graph-Level Representation Learning

## Overview

This paper presents GrePool...

### Python environment setup with Conda
```shell
conda create -n grepool python=3.10
conda install pytorch==2.1.0 torchvision==0.16.0 torchaudio==2.1.0 pytorch-cuda=11.8 -c pytorch -c nvidia
conda install pyg -c pyg
pip install rdkit-pypi cython
pip install ogb
pip install pyarrow
pip install transformers
pip install configargparse
pip install loguru
pip install wandb
pip install nvidia-ml-py3
pip install tensorboardX
pip install sortedcontainers
pip install pyyaml
pip install pyg_lib torch_scatter torch_sparse torch_cluster torch_spline_conv -f https://data.pyg.org/whl/torch-2.1.0+cu118.html
```

### Running GrePool (topk version for example)
```shell
conda activate grepool
cd gtpooling_basic_final/
# Running grepool topk version using DD dataset
sh run_tu_dd.sh 0
# Running grepool topk version using ogb datasets
sh run_ogb.sh 0
sh run_pcba.sh 0
```
Supported datasets:
- TuDataset: `NCI1`, `MUTAG`, `PTC-MR`, `NCI109`, `ENZYMES`, `Mutagenicity`, `IMDB-M`, `IMDB-B`
- OGB: `ogbg-molhiv`, `ogbg-moltox21`, `ogbg-moltoxcast`, `ogbg-molpcba`

## Datasets
Datasets mentioned above will be downloaded automatically using PyG's API when running the code.
