import os
import random
from datetime import datetime
import time
import shutil

import configargparse
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
# import wandb
from loguru import logger
from ogb.graphproppred import Evaluator, PygGraphPropPredDataset
from torch.optim.lr_scheduler import CosineAnnealingLR, OneCycleLR, ReduceLROnPlateau
from torch_geometric.data import DataLoader
from tqdm import tqdm

import utils
from dataset.adj_list import compute_adjacency_list_cached
from dataset import DATASET_UTILS
from models import get_model_and_parser
from trainers import get_trainer_and_parser

import sparselearning
from sparselearning.core import Masking, CosineDecay, LinearDecay

# wandb.init(project="graph-aug")
now = datetime.now()
now = now.strftime("%m_%d-%H_%M_%S")


class BaseTrainer:
    @staticmethod
    def transform(args):
        return None

    @staticmethod
    def add_args(parser):
        pass

    @staticmethod
    def train(model, device, loader, optimizer, args, calc_loss, mask, scheduler=None):
        model.train()

        loss_accum = 0
        t = tqdm(loader, desc="Train")
        for step, batch in enumerate(t):
            batch = batch.to(device)

            if batch.x.shape[0] == 1 or batch.batch[-1] == 0:
                pass
            else:
                optimizer.zero_grad()
                pred_list, noise_list = model(batch)

                loss = calc_loss(pred_list, batch, noise=noise_list, uni_rate=args.uniform_rate)

                loss.backward()
                if args.grad_clip is not None:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip)

                if mask is not None:
                    print("Mask!!!!")
                    mask.step()
                else:
                    optimizer.step()

                if scheduler:
                    scheduler.step()

                detached_loss = loss.item()
                loss_accum += detached_loss
                t.set_description(f"Train (loss = {detached_loss:.4f}, smoothed = {loss_accum / (step + 1):.4f})")
                # wandb.log({"train/iter-loss": detached_loss, "train/iter-loss-smoothed": loss_accum / (step + 1)})

        logger.info("Average training loss: {:.4f}".format(loss_accum / (step + 1)))
        return loss_accum / (step + 1)

    @staticmethod
    def name(args):
        return "base"
        # raise NotImplemented


def main():
    # fmt: off
    parser = configargparse.ArgumentParser(allow_abbrev=False,
                                           description='GNN baselines on ogbg-code data with Pytorch Geometrics')
    parser.add_argument('--configs', required=False, is_config_file=True)
    parser.add_argument('--wandb_run_idx', type=str, default=None)

    parser.add_argument('--data_root', type=str, default='../data')
    parser.add_argument('--dataset', type=str, default="ogbg-code",
                        help='dataset name (default: ogbg-code)')

    parser.add_argument('--aug', type=str, default='baseline',
                        help='augment method to use [baseline|flag|augment]')

    parser.add_argument('--max_seq_len', type=int, default=None,
                        help='maximum sequence length to predict (default: None)')

    group = parser.add_argument_group('model')
    group.add_argument('--model_type', type=str, default='gnn', help='gnn|pna|gnn-transformer')
    group.add_argument('--graph_pooling', type=str, default='mean')
    group = parser.add_argument_group('gnn')
    group.add_argument('--gnn_type', type=str, default='gcn')
    group.add_argument('--gnn_virtual_node', action='store_true')
    group.add_argument('--gnn_dropout', type=float, default=0)
    group.add_argument('--gnn_num_layer', type=int, default=5,
                       help='number of GNN message passing layers (default: 5)')
    group.add_argument('--gnn_emb_dim', type=int, default=300,
                       help='dimensionality of hidden units in GNNs (default: 300)')
    group.add_argument('--gnn_JK', type=str, default='last')
    group.add_argument('--gnn_residual', action='store_true', default=False)

    group = parser.add_argument_group('training')
    # group.add_argument('--devices', type=str, default="0",
    #                     help='which gpu to use if any (default: 0)')
    group.add_argument('--batch_size', type=int, default=128,
                       help='input batch size for training (default: 128)')
    group.add_argument('--eval_batch_size', type=int, default=None,
                       help='input batch size for training (default: train batch size)')
    group.add_argument('--epochs', type=int, default=30,
                       help='number of epochs to train (default: 30)')
    group.add_argument('--num_workers', type=int, default=0,
                       help='number of workers (default: 0)')
    group.add_argument('--scheduler', type=str, default=None)
    group.add_argument('--pct_start', type=float, default=0.3)
    group.add_argument('--weight_decay', type=float, default=0.0)
    group.add_argument('--grad_clip', type=float, default=None)
    group.add_argument('--lr', type=float, default=0.001)
    group.add_argument('--max_lr', type=float, default=0.001)
    group.add_argument('--runs', type=int, default=10)
    group.add_argument('--test-freq', type=int, default=1)
    group.add_argument('--start-eval', type=int, default=15)
    group.add_argument('--resume', type=str, default=None)
    group.add_argument('--seed', type=int, default=None)
    group.add_argument('--device', type=int, default=0)
    # uniform-loss
    group.add_argument('--uniform_rate', type=float, default=0.01)

    group = parser.add_argument_group('sparse')
    group.add_argument('--adj_sparse', action='store_true', help='If Sparse Adj.')
    group.add_argument('--feature_sparse', action='store_true', help='If Sparse Weight.')
    group.add_argument('--weight_sparse', action='store_true', help='If Sparse Feature.', )
    group.add_argument('--multiplier', type=int, default=1, metavar='N',
                       help='extend training time by multiplier times')
    group.add_argument('--decay-schedule', type=str, default='cosine',
                       help='The decay schedule for the pruning rate. Default: cosine. Choose from: cosine, linear.')
    group.add_argument('--growth_schedule', type=str, default='gradient',
                       help='The growth schedule. Default: gradient. Choose from: gradient, momentum, random.')

    # fmt: on

    sparselearning.core.add_sparse_args(parser)

    args, _ = parser.parse_known_args()

    dataset_util = DATASET_UTILS[args.dataset]()
    dataset_util.add_args(parser)
    args, _ = parser.parse_known_args()

    # Setup Trainer and add customized args
    # trainer = get_trainer_and_parser(args, parser)
    trainer = BaseTrainer()
    train = trainer.train
    model_cls = get_model_and_parser(args, parser)
    args = parser.parse_args()
    data_transform = trainer.transform(args)

    run_name = f"{args.dataset}+{model_cls.name(args)}"
    run_name += f"+{trainer.name(args)}+lr={args.lr}+wd={args.weight_decay}"
    if args.scheduler is not None:
        run_name = run_name + f"+sch={args.scheduler}"
    if args.seed:
        run_name = run_name + f"+seed{args.seed}"
    if args.wandb_run_idx is not None:
        run_name = args.wandb_run_idx + "_" + run_name

    # wandb.run.name = run_name

    device = torch.device('cuda:{}'.format(args.device) if torch.cuda.is_available() else "cpu")

    args.device = torch.device('cuda:{}'.format(args.device) if torch.cuda.is_available() else "cpu")

    # device = torch.device("cuda") if torch.cuda.is_available() and args.devices else torch.device("cpu")
    args.save_path = f"exps/{run_name}-{now}"
    os.makedirs(args.save_path, exist_ok=True)
    if args.resume is not None:
        args.save_path = args.resume
    logger.info(args)
    # wandb.config.update(args)

    utils.seed_everything(args.seed)

    if "ogb" in args.dataset:
        # automatic dataloading and splitting
        dataset_ = PygGraphPropPredDataset(name=args.dataset, root=args.data_root, transform=data_transform)
        dataset_eval_ = PygGraphPropPredDataset(name=args.dataset, root=args.data_root)
        num_tasks, node_encoder_cls, edge_encoder_cls, deg = dataset_util.preprocess(dataset_, dataset_eval_, model_cls,
                                                                                     args)
        evaluator = Evaluator(args.dataset)  # automatic evaluator. takes dataset name as input
    else:
        dataset_, num_tasks, node_encoder_cls, edge_encoder_cls, deg = dataset_util.preprocess(args)
        dataset_eval_ = dataset_
        evaluator = None

    task_type = dataset_.task_type
    split_idx = dataset_.get_idx_split()
    calc_loss = dataset_util.loss_fn(task_type)
    eval = dataset_util.eval

    def create_loader(dataset, dataset_eval):
        test_data = compute_adjacency_list_cached(dataset[split_idx["test"]], key=f"{args.dataset}_test")
        valid_data = compute_adjacency_list_cached(dataset_eval[split_idx["valid"]], key=f"{args.dataset}_valid")
        train_data = compute_adjacency_list_cached(dataset[split_idx["train"]], key=f"{args.dataset}_train")
        logger.debug("Finished computing adjacency list")

        eval_bs = args.batch_size if args.eval_batch_size is None else args.eval_batch_size
        train_loader = DataLoader(train_data, batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers,
                                  pin_memory=True)
        train_loader_eval = DataLoader(train_data, batch_size=eval_bs, shuffle=False, num_workers=args.num_workers,
                                       pin_memory=True)
        valid_loader = DataLoader(valid_data, batch_size=eval_bs, shuffle=False, num_workers=args.num_workers,
                                  pin_memory=True)
        test_loader = DataLoader(test_data, batch_size=eval_bs, shuffle=False, num_workers=args.num_workers,
                                 pin_memory=True)
        return train_loader, train_loader_eval, valid_loader, test_loader

    train_loader_, train_loader_eval_, valid_loader_, test_loader_ = create_loader(dataset_, dataset_eval_)

    def count_parameters(model):
        return sum(p.numel() for p in model.parameters() if p.requires_grad)

    def run(run_id):
        if "ogb" not in args.dataset:
            dataset, _, _, _, _ = dataset_util.preprocess(args)
            dataset_eval = dataset
            train_loader, train_loader_eval, valid_loader, test_loader = create_loader(dataset, dataset_eval)
        else:
            train_loader, train_loader_eval, valid_loader, test_loader = train_loader_, train_loader_eval_, valid_loader_, test_loader_
            dataset = dataset_
        node_encoder = node_encoder_cls()

        os.makedirs(os.path.join(args.save_path, str(run_id)), exist_ok=True)
        best_val, final_test = 0, 0
        model = model_cls(num_tasks=num_tasks, args=args, node_encoder=node_encoder,
                          edge_encoder_cls=edge_encoder_cls).to(device)
        print("Model Parameters: ", count_parameters(model))
        # exit(-1)
        # model = nn.DataParallel(model)

        # wandb.watch(model)

        optimizer = optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
        if args.scheduler == "plateau":
            # NOTE(ajayjain): For Molhiv config, this min_lr is too high -- means that lr does not decay.
            scheduler = ReduceLROnPlateau(optimizer, mode="min", factor=0.5, patience=20, min_lr=0.0001, verbose=False)
        elif args.scheduler == "cosine":
            scheduler = CosineAnnealingLR(optimizer, T_max=args.epochs * len(train_loader), verbose=False)
        elif args.scheduler == "onecycle":
            scheduler = OneCycleLR(
                optimizer,
                max_lr=args.max_lr,
                epochs=args.epochs,
                steps_per_epoch=len(train_loader),
                pct_start=args.pct_start,
                verbose=False,
            )
        elif args.scheduler is None:
            scheduler = None
        else:
            raise NotImplementedError

        mask = None
        if args.sparse:
            decay = CosineDecay(args.prune_rate, len(train_loader) * (args.epochs * args.multiplier))
            mask = Masking(optimizer, prune_rate=args.prune_rate, death_mode=args.prune, prune_rate_decay=decay,
                           growth_mode=args.growth,
                           redistribution_mode=args.redistribution, args=args, train_loader=train_loader, device=device)
            mask.add_module(model, sparse_init=args.sparse_init)

        # Load resume model, if any
        t0 = time.time()
        per_epoch_time = []

        start_epoch = 1
        last_model_path = os.path.join(args.save_path, str(run_id), "last_model.pt")
        if os.path.exists(last_model_path):
            state_dict = torch.load(last_model_path)
            start_epoch = state_dict["epoch"] + 1
            model.load_state_dict(state_dict["model"])
            optimizer.load_state_dict(state_dict["optimizer"])
            if args.scheduler:
                scheduler.load_state_dict(state_dict["scheduler"])
            logger.info("[Resume] Loaded: {last_model_path} epoch: {start_epoch}")

        model.epoch_callback(epoch=start_epoch - 1)
        for epoch in range(start_epoch, args.epochs + 1):
            logger.info(f"=====Epoch {epoch}=====")
            start = time.time()
            # logger.info("Training...")
            # logger.info("Total parameters: {}", utils.num_total_parameters(model))
            # logger.info("Trainable parameters: {}", utils.num_trainable_parameters(model))
            loss = train(model, device, train_loader, optimizer, args, calc_loss, mask,
                         scheduler if args.scheduler != "plateau" else None)

            model.epoch_callback(epoch)
            # wandb.log({f"train/loss-runs{run_id}": loss, f"train/lr": optimizer.param_groups[0]["lr"], f"epoch": epoch})

            if args.scheduler == "plateau":
                valid_perf = eval(model, device, valid_loader, evaluator)
                valid_metric = valid_perf[dataset.eval_metric]
                scheduler.step(valid_metric)
            if epoch > args.start_eval and epoch % args.test_freq == 0 or epoch in [1, args.epochs]:
                logger.info("Evaluating...")
                with torch.no_grad():
                    train_perf = eval(model, device, train_loader_eval, evaluator)
                    if args.scheduler != "plateau":
                        valid_perf = eval(model, device, valid_loader, evaluator)
                    test_perf = eval(model, device, test_loader, evaluator)

                train_metric, valid_metric, test_metric = (
                    train_perf[dataset.eval_metric],
                    valid_perf[dataset.eval_metric],
                    test_perf[dataset.eval_metric],
                )
                # wandb.log(
                #     {
                #         f"train/{dataset.eval_metric}-runs{run_id}": train_metric,
                #         f"valid/{dataset.eval_metric}-runs{run_id}": valid_metric,
                #         f"test/{dataset.eval_metric}-runs{run_id}": test_metric,
                #         "epoch": epoch,
                #     }
                # )
                # logger.info(f"Running: {run_name} (runs {run_id})")
                # logger.info(f"Run {run_id} - train: {train_metric}, val: {valid_metric}, test: {test_metric}")

                # Save checkpoints
                state_dict = {"model": model.state_dict(), "optimizer": optimizer.state_dict(), "epoch": epoch}
                state_dict["scheduler"] = scheduler.state_dict() if args.scheduler else None
                torch.save(state_dict, os.path.join(args.save_path, str(run_id), "last_model.pt"))
                # logger.info("[Save] Save model: {}", os.path.join(args.save_path, str(run_id), "last_model.pt"))
                if best_val < valid_metric:
                    best_val = valid_metric
                    final_test = test_metric
                    # wandb.run.summary[f"best/valid/{dataset.eval_metric}-runs{run_id}"] = valid_metric
                    # wandb.run.summary[f"best/test/{dataset.eval_metric}-runs{run_id}"] = test_metric
                    torch.save(state_dict, os.path.join(args.save_path, str(run_id), "best_model.pt"))
                    # logger.info("[Best Model] Save model: {}", os.path.join(args.save_path, str(run_id), "best_model.pt"))
            per_epoch_time.append(time.time() - start)

        state_dict = torch.load(os.path.join(args.save_path, str(run_id), "best_model.pt"))
        logger.info("[Evaluate] Loaded from {}", os.path.join(args.save_path, str(run_id), "best_model.pt"))
        model.load_state_dict(state_dict["model"])
        best_valid_perf = eval(model, device, valid_loader, evaluator)
        best_test_perf = eval(model, device, test_loader, evaluator)
        # delete after evaluation
        shutil.rmtree(os.path.join(args.save_path, str(run_id)))
        total_time_taken = time.time() - t0
        avg_time_epoch = np.mean(per_epoch_time)

        return best_valid_perf[dataset.eval_metric], best_test_perf[
            dataset.eval_metric], total_time_taken, avg_time_epoch

    vals, tests, total_time_list, avg_time_list = [], [], [], []
    for run_id in range(args.runs):
        best_val, final_test, total_time, avg_time = run(run_id)
        vals.append(best_val)
        tests.append(final_test)
        total_time_list.append(total_time)
        avg_time_list.append(avg_time)

        logger.info(f"Run {run_id} - val: {best_val}, test: {final_test}")
        # processing results
        # utils.results_to_file(args, final_test, 0,
        #                       best_val, 0,
        #                       total_time, 0,
        #                       avg_time, 0)

    logger.info(f"Average val accuracy: {np.mean(vals)} ± {np.std(vals)}")
    logger.info(f"Average test accuracy: {np.mean(tests)} ± {np.std(tests)}")
    logger.info(f"Average total time: {np.mean(total_time_list)} ± {np.std(total_time_list)}")
    logger.info(f"Average epoch time: {np.mean(avg_time_list)} ± {np.std(avg_time_list)}")
    # final results
    utils.results_to_file(args, np.mean(tests), np.std(tests),
                          np.mean(vals), np.std(vals),
                          np.mean(total_time_list), np.std(total_time_list),
                          np.mean(avg_time_list), np.std(avg_time_list))
    # delete after all
    shutil.rmtree(args.save_path)


if __name__ == "__main__":
    main()