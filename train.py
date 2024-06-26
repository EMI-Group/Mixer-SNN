import argparse
import random
import time
import warnings
import datetime

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
import torch.utils.data
from torch.utils.tensorboard import SummaryWriter
import numpy as np
import os
import sys
import shutil

import torchvision.datasets

import models.mixers_sparse_patchcell_layer_norm
import models.mixers_sparse_patchcell_tdbn
import models.mixers_sparse_patchcell_tebn
import models.mixers_sparse_patchcell
import models.mixers_sparse_patchcell_origin
import models.configs
import utils
import models.layers

from spikingjelly.activation_based import functional, monitor, neuron

from timm.data.constants import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD
from timm.data import create_transform
from timm.data import Mixup
from timm.loss import SoftTargetCrossEntropy

from samplers import RASampler


def set_deterministic(_seed_: int = 2022):
    random.seed(_seed_)
    np.random.seed(_seed_)
    torch.manual_seed(_seed_)
    torch.cuda.manual_seed_all(_seed_)
    cudnn.deterministic = True
    cudnn.benchmark = False


CONFIG_MAP = {
    "tiny": models.configs.get_mixer_sparse_tiny_config(),
    "small": models.configs.get_mixer_sparse_small_config(),
    "big": models.configs.get_mixer_sparse_big_config()
}


class Trainer(object):
    def main(self, args):
        self.models = {
            'mixer_sparse': {
                'model': models.mixers_sparse_patchcell_origin.sMLPNet,
                'config': CONFIG_MAP[args.model_size]
            }
        }
        set_deterministic(args.seed)
        if args.output_dir:
            os.makedirs(args.output_dir, exist_ok=True)

        utils.init_distributed_mode(args)
        print(args)

        device = torch.device(args.device)

        dataset_train, dataset_test, train_sampler, test_sampler = self.load_data(args)

        num_classes = len(dataset_train.classes)

        args.num_classes = num_classes

        dataloader_train = torch.utils.data.DataLoader(
            dataset=dataset_train,
            batch_size=args.batch_size,
            sampler=train_sampler,
            num_workers=args.workers,
            pin_memory=True,
            drop_last=True
        )

        dataloader_test = torch.utils.data.DataLoader(
            dataset=dataset_test,
            batch_size=args.batch_size,
            sampler=test_sampler,
            num_workers=args.workers,
            pin_memory=True,
            drop_last=False
        )

        mixup_fn = None
        mixup_active = args.mixup > 0 or args.cutmix > 0. or args.cutmix_minmax is not None
        if mixup_active:
            mixup_fn = Mixup(
                mixup_alpha=args.mixup, cutmix_alpha=args.cutmix, cutmix_minmax=args.cutmix_minmax,
                prob=args.mixup_prob, switch_prob=args.mixup_switch_prob, mode=args.mixup_mode,
                label_smoothing=args.smoothing, num_classes=args.num_classes)

        print('Creating model...')
        model = self.load_model(args, num_classes)
        # model = models.layers.convert_bn_to_sync_bn(model)
        model.to(device)
        print(model)

        criterion = self.set_criterion(args)

        optimizer = self.set_optimizer(args, model.parameters())

        if args.amp:
            scaler = torch.cuda.amp.GradScaler()
        else:
            scaler = None

        lr_scheduler = self.set_lr_scheduler(args, optimizer)

        model_without_ddp = model
        if args.distributed:
            model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
            model_without_ddp = model.module

        log_dir = os.path.join(args.output_dir, self.get_logdir_name(args))
        pt_dir = os.path.join(log_dir, 'pt')
        tb_dir = os.path.join(log_dir, 'tb')
        print(log_dir)

        if utils.is_main_process() and args.clean and os.path.exists(log_dir):
            shutil.rmtree(log_dir)

        if utils.is_main_process():
            os.makedirs(tb_dir, exist_ok=args.resume is not None)
            os.makedirs(pt_dir, exist_ok=args.resume is not None)

        max_test_acc1 = -1.
        if args.resume is not None:
            if args.resume == 'latest':
                checkpoint = torch.load(os.path.join(pt_dir, 'checkpoint_latest.pth'), map_location='cpu')
            else:
                checkpoint = torch.load(args.resume, map_location='cpu')
            model_without_ddp.load_state_dict(checkpoint['model'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
            args.start_epoch = checkpoint['epoch'] + 1
            if scaler:
                scaler.load_state_dict(checkpoint['scaler'])

            if utils.is_main_process():
                max_test_acc1 = checkpoint['max_test_acc1']

            print('Resume...')
            print(f'max_test_acc1: {max_test_acc1}')

        if args.fine_tune is not None:
            checkpoint = torch.load(args.fine_tune, map_location='cpu')
            model_state_dict = utils.fine_tune_state_dict(checkpoint['model'], model_without_ddp.model[-1])
            model_without_ddp.load_state_dict(model_state_dict)

            print('Fine tune...')
            print(f'Pre-train model: {args.fine_tune}')

        if utils.is_main_process():
            tb_writer = SummaryWriter(tb_dir, purge_step=args.start_epoch)
            self.save_args(args, log_dir)

        if args.test_only:
            if args.record_fire_rate:
                fr_monitor = monitor.OutputMonitor(model, neuron.LIFNode, utils.cal_fire_rate)

            test_loss, test_acc1, test_acc5 = self.evaluate(args, model, criterion, dataloader_test, device)
            eval_result = {
                'test_loss': test_loss,
                'test_acc1': test_acc1,
                'test_acc5': test_acc5,
            }
            if args.record_fire_rate:
                eval_result['fr_records'] = {layer: torch.mean(torch.cat([r.unsqueeze(0) for r in fr_monitor[layer]], dim=0), dim=0) for layer in fr_monitor.monitored_layers}
            utils.save_on_master(eval_result, os.path.join(log_dir, 'eval_result.pth'))

            if args.record_fire_rate:
                fr_monitor.remove_hooks()
                del fr_monitor

            return

        for epoch in range(args.start_epoch, args.epochs):
            start_time = time.time()
            if args.distributed:
                train_sampler.set_epoch(epoch)

            train_loss, train_acc1, train_acc5 = self.train_one_epoch(model, criterion, optimizer, dataloader_train,
                                                                      device, epoch, args, scaler, mixup_fn)
            if utils.is_main_process():
                tb_writer.add_scalar('train_loss', train_loss, epoch)
                tb_writer.add_scalar('train_acc1', train_acc1, epoch)
                tb_writer.add_scalar('train_acc5', train_acc5, epoch)

            lr_scheduler.step()
            test_loss, test_acc1, test_acc5 = self.evaluate(args, model, dataloader_test, device)
            if utils.is_main_process():
                tb_writer.add_scalar('test_loss', test_loss, epoch)
                tb_writer.add_scalar('test_acc1', test_acc1, epoch)
                tb_writer.add_scalar('test_acc5', test_acc5, epoch)

            if utils.is_main_process():
                save_max_test_acc1 = False
                if test_acc1 > max_test_acc1:
                    max_test_acc1 = test_acc1
                    save_max_test_acc1 = True
                checkpoint = {
                    "model": model_without_ddp.state_dict(),
                    "optimizer": optimizer.state_dict(),
                    "lr_scheduler": lr_scheduler.state_dict(),
                    "epoch": epoch,
                    "args": args,
                    "max_test_acc1": max_test_acc1,
                }
                if scaler:
                    checkpoint["scaler"] = scaler.state_dict()
                utils.save_on_master(checkpoint, os.path.join(pt_dir, "checkpoint_latest.pth"))
                if save_max_test_acc1:
                    utils.save_on_master(checkpoint, os.path.join(pt_dir, f"checkpoint_max_test_acc1.pth"))

            print(
                f'escape time={(datetime.datetime.now() + datetime.timedelta(seconds=(time.time() - start_time) * (args.epochs - epoch))).strftime("%Y-%m-%d %H:%M:%S")}\n')
            print(args)

    def train_one_epoch(self, model, criterion, optimizer, data_loader, device, epoch, args, scaler, mixup_fn):
        model.train()
        metric_logger = utils.MetricLogger(delimiter=' ')
        metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value}'))
        metric_logger.add_meter('img/s', utils.SmoothedValue(window_size=10, fmt='{value}'))

        header = f'Epoch: [{epoch}]'
        for i, (img, target) in enumerate(metric_logger.log_every(data_loader, -1, header)):
            start_time = time.time()
            img = img.to(device, non_blocking=True)
            target = target.to(device, non_blocking=True)
            img, target = mixup_fn(img, target)
            with torch.cuda.amp.autocast(enabled=scaler is not None):
                img = self.preprocess_train_sample(args, img)
                output = self.process_model_output(args, model(img))
                loss = self.cal_loss(args, criterion, output, target)

            optimizer.zero_grad()
            if scaler is not None:
                scaler.scale(loss).backward()
                if args.clip_grad_norm is not None:
                    scaler.unscale_(optimizer)
                    nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad_norm)
                scaler.step(optimizer)
                scaler.update()
            else:
                loss.backward()
                if args.clip_grad_norm is not None:
                    nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad_norm)
                optimizer.step()
            functional.reset_net(model)

            acc1, acc5 = self.cal_acc1_acc5(output, target)
            batch_size = target.shape[0]
            metric_logger.update(loss=loss.item(), lr=optimizer.param_groups[0]['lr'])
            metric_logger.meters['acc1'].update(acc1.item(), n=batch_size)
            metric_logger.meters['acc5'].update(acc5.item(), n=batch_size)
            metric_logger.meters['img/s'].update(batch_size / (time.time() - start_time))
            print(
                f'Train[{i}/{len(data_loader)}]: train_acc1={acc1:.3f}, train_acc5={acc5:.3f}, train_loss={loss:.6f}, lr={optimizer.param_groups[0]["lr"]}')
        metric_logger.synchronize_between_processes()
        train_loss, train_acc1, train_acc5 = metric_logger.loss.global_avg, metric_logger.acc1.global_avg, metric_logger.acc5.global_avg
        print(
            f'Train: train_acc1={train_acc1:.3f}, train_acc5={train_acc5:.3f}, train_loss={train_loss:.6f}, samples/s={metric_logger.meters["img/s"]}, lr={metric_logger.lr.value}')
        return train_loss, train_acc1, train_acc5

    @torch.no_grad()
    def evaluate(self, args, model, data_loader, device, log_suffix=""):
        criterion = torch.nn.CrossEntropyLoss()
        model.eval()
        metric_logger = utils.MetricLogger(delimiter=' ')
        header = f'Test: {log_suffix}'

        num_processed_samples = 0
        start_time = time.time()
        with torch.inference_mode():
            for img, target in metric_logger.log_every(data_loader, -1, header):
                img = img.to(device, non_blocking=True)
                target = target.to(device, non_blocking=True)
                img = self.preprocess_test_sample(args, img)
                output = self.process_model_output(args, model(img))
                loss = self.cal_loss(args, criterion, output, target)

                acc1, acc5 = self.cal_acc1_acc5(output, target)

                batch_size = target.shape[0]
                metric_logger.update(loss=loss.item())
                metric_logger.meters['acc1'].update(acc1.item(), n=batch_size)
                metric_logger.meters['acc5'].update(acc5.item(), n=batch_size)
                num_processed_samples += batch_size
                functional.reset_net(model)

        num_processed_samples = utils.reduce_across_processes(num_processed_samples)
        if (
                hasattr(data_loader.dataset, '__len__')
                and len(data_loader.dataset) != num_processed_samples
                and utils.is_main_process()
        ):
            warnings.warn(
                f"It looks like the dataset has {len(data_loader.dataset)} samples, but {num_processed_samples} "
                "samples were used for the validation, which might bias the results. "
                "Try adjusting the batch size and / or the world size. "
                "Setting the world size to 1 is always a safe bet."
            )

        metric_logger.synchronize_between_processes()

        test_loss, test_acc1, test_acc5 = metric_logger.loss.global_avg, metric_logger.acc1.global_avg, metric_logger.acc5.global_avg
        print(
            f'Test: test_acc1={test_acc1:.3f}, test_acc5={test_acc5:.3f}, test_loss={test_loss:.6f}, samples/s={num_processed_samples / (time.time() - start_time):.3f}')

        return test_loss, test_acc1, test_acc5

    def preprocess_train_sample(self, args, x):
        x = x.unsqueeze(0).repeat(args.T, 1, 1, 1, 1)
        return x

    def preprocess_test_sample(self, args, x):
        x = x.unsqueeze(0).repeat(args.T, 1, 1, 1, 1)
        return x

    def process_model_output(self, args, y):
        return y.mean(0) if args.criterion != 'tet' else y

    def cal_acc1_acc5(self, output, target):
        if args.criterion == 'tet':
            output = output.mean(0)
        acc1, acc5 = utils.accuracy(output, target, topk=(1, 5))
        return acc1, acc5

    def load_data(self, args):
        if args.data == 'imagenet':
            return self.load_ImageNet(args)
        elif args.data == 'cifar10':
            return self.load_CIFAR10(args)
        else:
            raise NotImplementedError()

    def load_model(self, args, num_classes):
        model_dict = self.models[args.model]
        config = model_dict['config']
        config.num_classes = num_classes
        model = model_dict['model'](**dict(config))
        functional.set_step_mode(model, 'm')
        if args.cupy:
            functional.set_backend(model, 'cupy')
        num_params = utils.count_parameters(model)
        print("Total Parameter: \t%2.1fM" % num_params)
        return model

    def set_optimizer(self, args, parameters):
        opt_name = args.opt.lower()
        if opt_name == 'sgd':
            optimizer = torch.optim.SGD(
                parameters,
                lr=args.lr,
                momentum=args.momentum,
                weight_decay=args.weight_decay
            )
        elif opt_name == 'adam':
            optimizer = torch.optim.Adam(
                parameters,
                lr=args.lr,
                weight_decay=args.weight_decay,
                betas=args.betas
            )
        elif opt_name == 'adamw':
            optimizer = torch.optim.AdamW(
                parameters,
                lr=args.lr,
                weight_decay=args.weight_decay,
                betas=args.betas
            )
        else:
            raise NotImplementedError(f'Not supported optimizer {args.opt}')
        return optimizer

    def set_lr_scheduler(self, args, optimizer):
        lr_scheduler = args.lr_scheduler.lower()
        if lr_scheduler == 'step':
            main_lr_scheduler = torch.optim.lr_scheduler.StepLR(
                optimizer,
                step_size=args.lr_step_size,
                gamma=args.lr_gamma
            )
        elif lr_scheduler == 'cosa':
            main_lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
                optimizer,
                T_max=args.epochs - args.lr_warmup_epochs
            )
        elif lr_scheduler == 'exp':
            main_lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(
                optimizer,
                gamma=args.lr_gamma
            )
        else:
            raise NotImplementedError(f'Not supported lr_scheduler {args.lr_scheduler}')
        if args.lr_warmup_epochs > 0:
            if args.lr_warmup_method == 'linear':
                warmup_lr_scheduler = torch.optim.lr_scheduler.LinearLR(
                    optimizer,
                    start_factor=args.lr_warmup_decay,
                    total_iters=args.lr_warmup_epochs
                )
            elif args.lr_warmup_method == 'constant':
                warmup_lr_scheduler = torch.optim.lr_scheduler.ConstantLR(
                    optimizer,
                    factor=args.lr_warmup_decay,
                    total_iters=args.lr_warmup_epochs
                )
            else:
                raise NotImplementedError(f'Not supported lr_warmup_method {args.lr_warmup_method}')
            lr_scheduler = torch.optim.lr_scheduler.SequentialLR(
                optimizer,
                schedulers=[warmup_lr_scheduler, main_lr_scheduler],
                milestones=[args.lr_warmup_epochs]
            )
        else:
            lr_scheduler = main_lr_scheduler

        return lr_scheduler

    def set_criterion(self, args):
        if args.criterion == 'mse':
            return nn.MSELoss()
        elif args.criterion == 'ce':
            return SoftTargetCrossEntropy()
        elif args.criterion == 'tet':
            return nn.MSELoss(), nn.CrossEntropyLoss()
        else:
            raise NotImplementedError()

    def cal_loss(self, args, criterion, outputs, targets):
        if args.criterion == 'mse':
            targets = F.one_hot(targets, num_classes=args.num_classes).float().cuda()
            return criterion(outputs, targets)
        elif args.criterion == 'ce':
            return criterion(outputs, targets)
        elif args.criterion == 'tet':
            mse_loss, ce_loss = criterion
            loss = 0
            MSE_PHI = torch.ones(args.num_classes).cuda() * 1.0
            TET_lambda = 5e-2
            for o in outputs:
                mse = MSE_PHI.expand((len(o), args.num_classes))
                loss += (1 - TET_lambda) * ce_loss(o, targets) + TET_lambda * mse_loss(o, mse)
            return loss
        else:
            raise NotImplementedError()

    def get_logdir_name(self, args):
        dir_name = f'{args.exp_name}_' \
                   f'{args.data}_' \
                   f'{args.model}_' \
                   f'T{args.T}_' \
                   f'b{args.batch_size}_' \
                   f'e{args.epochs}_' \
                   f'{args.opt}_' \
                   f'lr{args.lr}_' \
                   f'seed{args.seed}'
        return dir_name

    def save_args(self, args, log_dir):
        with open(os.path.join(log_dir, 'args.txt'), 'w', encoding='utf-8') as args_txt:
            args_txt.write(str(args))
            args_txt.write('\n')
            args_txt.write(' '.join(sys.argv))
            args_txt.write('\n')
            args_txt.write(str(self.models[args.model]['config']))

    def load_CIFAR10(self, args):
        print('Loading CIFAR10 Data...')
        dataset_train = torchvision.datasets.CIFAR10(
            root=args.data_path,
            download=True,
            train=True,
            transform=torchvision.transforms.Compose([
                torchvision.transforms.RandomResizedCrop(224),
                torchvision.transforms.RandomHorizontalFlip(),
                torchvision.transforms.ToTensor(),
                torchvision.transforms.Normalize(
                    mean=(0.4914, 0.4822, 0.4465),
                    std=(0.2023, 0.1994, 0.2010),
                )
            ]),
        )
        dataset_test = torchvision.datasets.CIFAR10(
            root=args.data_path,
            download=True,
            train=False,
            transform=torchvision.transforms.Compose([
                torchvision.transforms.Resize(size=(224, 224)),
                torchvision.transforms.ToTensor(),
                torchvision.transforms.Normalize(
                    mean=(0.4914, 0.4822, 0.4465),
                    std=(0.2023, 0.1994, 0.2010),
                )
            ]),
        )

        loader_generator = torch.Generator()
        loader_generator.manual_seed(args.seed)

        if args.distributed:
            train_sampler = torch.utils.data.DistributedSampler(dataset=dataset_train, seed=args.seed)
            test_sampler = torch.utils.data.DistributedSampler(dataset=dataset_test, shuffle=False)
        else:
            train_sampler = torch.utils.data.RandomSampler(data_source=dataset_train, generator=loader_generator)
            test_sampler = torch.utils.data.SequentialSampler(data_source=dataset_test)

        return dataset_train, dataset_test, train_sampler, test_sampler

    def load_ImageNet(self, args):
        def build_transform(is_train, args):
            resize_im = args.input_size > 32
            if is_train:
                transform = create_transform(
                    input_size=args.input_size,
                    is_training=True,
                    color_jitter=args.color_jitter,
                    auto_augment=args.aa,
                    interpolation=args.train_interpolation,
                    re_prob=args.reprob,
                    re_mode=args.remode,
                    re_count=args.recount,
                )
                if not resize_im:
                    transform.transforms[0] = torchvision.transforms.RandomCrop(
                        args.input_size, padding=4)
                return transform

            t = []
            if resize_im:
                size = int((256 / 224) * args.input_size)
                t.append(
                    torchvision.transforms.Resize(size, interpolation=3),
                )
                t.append(torchvision.transforms.CenterCrop(args.input_size))

            t.append(torchvision.transforms.ToTensor())
            t.append(torchvision.transforms.Normalize(IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD))
            return torchvision.transforms.Compose(t)

        print('Loading ImageNet Data...')
        train_path = os.path.join(args.data_path, 'train')
        val_path = os.path.join(args.data_path, 'val')

        train_transform = build_transform(True, args)
        val_transform = build_transform(False, args)
        dataset_train = torchvision.datasets.ImageFolder(root=train_path, transform=train_transform)
        dataset_val = torchvision.datasets.ImageFolder(root=val_path, transform=val_transform)

        num_tasks = utils.get_world_size()
        global_rank = utils.get_rank()
        if args.repeated_aug:
            sampler_train = RASampler(
                dataset_train, num_replicas=num_tasks, rank=global_rank, shuffle=True
            )
        else:
            sampler_train = torch.utils.data.DistributedSampler(
                dataset_train, num_replicas=num_tasks, rank=global_rank, shuffle=True
            )
        if args.dist_eval:
            if len(dataset_val) % num_tasks != 0:
                warnings.warn('Warning: Enabling distributed evaluation with an eval dataset not divisible by process number. '
                            'This will slightly alter validation results as extra duplicate entries are added to achieve '
                            'equal num of samples per-process.')
            sampler_val = torch.utils.data.DistributedSampler(
                dataset_val, num_replicas=num_tasks, rank=global_rank, shuffle=False)
        else:
            sampler_val = torch.utils.data.SequentialSampler(dataset_val)

        # train_transform = torchvision.transforms.Compose([
        #     torchvision.transforms.RandomResizedCrop(224),
        #     torchvision.transforms.RandomHorizontalFlip(),
        #     torchvision.transforms.ToTensor(),
        #     torchvision.transforms.Normalize(
        #         mean=[0.485, 0.456, 0.406],
        #         std=[0.229, 0.224, 0.225]
        #     )
        # ])
        #
        # val_transform = torchvision.transforms.Compose([
        #     torchvision.transforms.Resize((224, 224)),
        #     torchvision.transforms.ToTensor(),
        #     torchvision.transforms.Normalize(
        #         mean=[0.485, 0.456, 0.406],
        #         std=[0.229, 0.224, 0.225]
        #     )
        # ])
        #
        # dataset_train = torchvision.datasets.ImageFolder(root=train_path, transform=train_transform)
        # dataset_val = torchvision.datasets.ImageFolder(root=val_path, transform=val_transform)
        #
        # loader_generator = torch.Generator()
        # loader_generator.manual_seed(args.seed)
        #
        # if args.distributed:
        #     train_sampler = torch.utils.data.DistributedSampler(dataset=dataset_train, seed=args.seed)
        #     val_sampler = torch.utils.data.DistributedSampler(dataset=dataset_val, shuffle=False)
        # else:
        #     train_sampler = torch.utils.data.RandomSampler(data_source=dataset_train, generator=loader_generator)
        #     val_sampler = torch.utils.data.SequentialSampler(data_source=dataset_val)

        return dataset_train, dataset_val, sampler_train, sampler_val

    def get_args_parser(self):
        parser = argparse.ArgumentParser()

        parser.add_argument('--exp-name', default='mixer-exp', type=str)
        parser.add_argument('--data', default='cifar10', type=str)
        parser.add_argument('--data-path', default='./data', type=str)
        parser.add_argument('--input-size', default=224, type=int, help='images input size')
        parser.add_argument('--model_size', default='small', type=str, choices=['tiny', 'small', 'big'])
        parser.add_argument('--T', default=4, type=int)
        parser.add_argument('--cupy', action='store_true')
        parser.add_argument('--device', default='cuda', type=str)
        parser.add_argument('--batch-size', default=32, type=int)
        parser.add_argument('--epochs', default=90, type=int)
        parser.add_argument('--workers', default=1, type=int)
        parser.add_argument('--opt', default='sgd', type=str)
        parser.add_argument('--lr', default=0.1, type=float)
        parser.add_argument('--momentum', default=0.9, type=float)
        parser.add_argument('--weight-decay', default=0., type=float)
        parser.add_argument('--betas', default=[0.9, 0.999], type=float, nargs=2)
        parser.add_argument('--criterion', default='ce', type=str)
        parser.add_argument('--lr-scheduler', default='cosa', type=str)
        parser.add_argument('--lr-warmup-epochs', default=10, type=int)
        parser.add_argument('--lr-warmup-method', default='linear', type=str)
        parser.add_argument('--lr-warmup-decay', default=0.01, type=float)
        parser.add_argument('--lr-step-size', default=30, type=int)
        parser.add_argument('--lr-gamma', default=0.1, type=float)
        parser.add_argument('--output-dir', default='./logs', type=str)
        parser.add_argument('--resume', default=None, type=str)
        parser.add_argument('--start-epoch', default=0, type=int)
        parser.add_argument('--world-size', default=1, type=int)
        parser.add_argument('--dist-url', default='env://', type=str)
        parser.add_argument('--seed', default=42, type=int)
        parser.add_argument('--amp', action='store_true')
        parser.add_argument('--clip-grad-norm', default=None, type=float)
        parser.add_argument("--local-rank", type=int)
        parser.add_argument('--clean', action='store_true')
        parser.add_argument('--record-fire-rate', action='store_true')
        parser.add_argument('--test-only', action='store_true')
        parser.add_argument('--fine-tune', default=None, type=str)

        # Augmentation parameters
        parser.add_argument('--color-jitter', type=float, default=0.4, metavar='PCT',
                            help='Color jitter factor (default: 0.4)')
        parser.add_argument('--aa', type=str, default='rand-m9-mstd0.5-inc1', metavar='NAME',
                            help='Use AutoAugment policy. "v0" or "original". " + \
                                    "(default: rand-m9-mstd0.5-inc1)'),
        parser.add_argument('--smoothing', type=float, default=0.1, help='Label smoothing (default: 0.1)')
        parser.add_argument('--train-interpolation', type=str, default='bicubic',
                            help='Training interpolation (random, bilinear, bicubic default: "bicubic")')

        parser.add_argument('--repeated-aug', action='store_true')
        parser.add_argument('--no-repeated-aug', action='store_false', dest='repeated_aug')
        parser.set_defaults(repeated_aug=True)

        # * Random Erase params
        parser.add_argument('--reprob', type=float, default=0.25, metavar='PCT',
                            help='Random erase prob (default: 0.25)')
        parser.add_argument('--remode', type=str, default='pixel',
                            help='Random erase mode (default: "pixel")')
        parser.add_argument('--recount', type=int, default=1,
                            help='Random erase count (default: 1)')
        parser.add_argument('--resplit', action='store_true', default=False,
                            help='Do not random erase first (clean) augmentation split')

        # * Mixup params
        parser.add_argument('--mixup', type=float, default=0.8,
                            help='mixup alpha, mixup enabled if > 0. (default: 0.8)')
        parser.add_argument('--cutmix', type=float, default=1.0,
                            help='cutmix alpha, cutmix enabled if > 0. (default: 1.0)')
        parser.add_argument('--cutmix-minmax', type=float, nargs='+', default=None,
                            help='cutmix min/max ratio, overrides alpha and enables cutmix if set (default: None)')
        parser.add_argument('--mixup-prob', type=float, default=1.0,
                            help='Probability of performing mixup or cutmix when either/both is enabled')
        parser.add_argument('--mixup-switch-prob', type=float, default=0.5,
                            help='Probability of switching to cutmix when both mixup and cutmix enabled')
        parser.add_argument('--mixup-mode', type=str, default='batch',
                            help='How to apply mixup/cutmix params. Per "batch", "pair", or "elem"')

        parser.add_argument('--dist-eval', action='store_true', default=False, help='Enabling distributed evaluation')

        return parser


if __name__ == '__main__':
    trainer = Trainer()
    args = trainer.get_args_parser().parse_args()
    trainer.main(args)
