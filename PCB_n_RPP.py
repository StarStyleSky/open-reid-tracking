from __future__ import print_function, absolute_import
import argparse
import os.path as osp
import os

import numpy as np
import time
import datetime
import random
import sys
import torch
from torch import nn
from torch.backends import cudnn
from torch.utils.data import DataLoader
import matplotlib

matplotlib.use('agg')
import matplotlib.pyplot as plt
import json

from reid import datasets
from reid import models
from reid.trainers import Trainer
from reid.evaluators import Evaluator
from reid.utils.data import transforms as T
from reid.utils.data.preprocessor import Preprocessor
from reid.utils.logging import Logger
from reid.utils.serialization import load_checkpoint, save_checkpoint

# if os.name == 'nt':  # windows
#     num_workers = 0
#     batch_size = 64
#     pass
# else:  # linux
#     num_workers = 8
#     batch_size = 64
#     os.environ["CUDA_VISIBLE_DEVICES"] = '0, 1, 2, 3'

'''
    ideas for better training from Dr. Yifan Sun
    
    train resnet BN by default                              check
    no crop                                                 check
    batch_size = 64 , lr = 0.1                              check
    dropout -- possible at layer: pool5                     check
    skip step-3 in RPP training                             check
    RPP classifier -- 2048 -> 256 -> 6 (average pooling)    check
'''


def str2bool(v):
    return v.lower() in ('true')


def get_data(name, data_dir, height, width, batch_size, workers,
             combine_trainval, crop, mygt_icams, fps, re=0):
    root = osp.join(data_dir, name)

    if name == 'duke_my_gt':
        if mygt_icams != 0:
            mygt_icams = [mygt_icams]
        else:
            mygt_icams = list(range(1, 9))
        dataset = datasets.create(name, root, iCams=mygt_icams, fps=fps)
    else:
        dataset = datasets.create(name, root)

    normalizer = T.Normalize(mean=[0.485, 0.456, 0.406],
                             std=[0.229, 0.224, 0.225])

    train_set = dataset.trainval if combine_trainval else dataset.train
    num_classes = (dataset.num_trainval_ids if combine_trainval
                   else dataset.num_train_ids)

    if crop:  # default: False
        train_transformer = T.Compose([
            # T.Resize((int(height / 8 * 9), int(width / 8 * 9)), interpolation=3),
            # T.RandomCrop((height, width)),
            T.RandomSizedRectCrop(height, width, interpolation=3),
            T.RandomHorizontalFlip(),
            T.ToTensor(),
            normalizer,
            T.RandomErasing(EPSILON=re),
        ])
    else:
        train_transformer = T.Compose([
            T.RectScale(height, width, interpolation=3),
            T.RandomHorizontalFlip(),
            T.ToTensor(),
            normalizer,
            T.RandomErasing(EPSILON=re),
        ])

    test_transformer = T.Compose([
        # T.Resize((height, width), interpolation=3),
        T.RectScale(height, width, interpolation=3),
        T.ToTensor(),
        normalizer,
    ])

    train_loader = DataLoader(
        Preprocessor(train_set, root=dataset.images_dir,
                     transform=train_transformer),
        batch_size=batch_size, num_workers=workers,
        shuffle=True, pin_memory=True, drop_last=True)

    val_loader = DataLoader(
        Preprocessor(dataset.val, root=dataset.images_dir,
                     transform=test_transformer),
        batch_size=batch_size, num_workers=workers,
        shuffle=False, pin_memory=True)

    # slimmer & faster query
    indices_eval_query = random.sample(range(len(dataset.query)), int(len(dataset.query) / 5))
    eval_set_query = list(dataset.query[i] for i in indices_eval_query)

    query_loader = DataLoader(
        Preprocessor(dataset.query,
                     root=dataset.images_dir, transform=test_transformer),
        batch_size=batch_size, num_workers=workers,
        shuffle=False, pin_memory=True)

    gallery_loader = DataLoader(
        Preprocessor(dataset.gallery,
                     root=dataset.images_dir, transform=test_transformer),
        batch_size=batch_size, num_workers=workers,
        shuffle=False, pin_memory=True)

    return dataset, num_classes, train_loader, val_loader, query_loader, gallery_loader


def checkpoint_loader(model, path, eval_only=False):
    checkpoint = load_checkpoint(path)
    pretrained_dict = checkpoint['state_dict']
    if isinstance(model, nn.DataParallel):
        Parallel = 1
        model = model.module.cpu()
    else:
        Parallel = 0
    if 'rpp' in checkpoint:
        has_rpp = checkpoint['rpp']
        if has_rpp:
            if isinstance(model, nn.DataParallel):
                model.module.enable_RPP()
            else:
                model.enable_RPP()

    model_dict = model.state_dict()
    # 1. filter out unnecessary keys
    pretrained_dict = {k: v for k, v in pretrained_dict.items() if k in model_dict}
    if eval_only:
        keys = pretrained_dict.keys()
        keys_to_del = []
        for key in keys:
            if 'fc' in key:
                keys_to_del.append(key)
        for key in keys_to_del:
            del pretrained_dict[key]
        pass

    # 2. overwrite entries in the existing state dict
    model_dict.update(pretrained_dict)
    # 3. load the new state dict
    model.load_state_dict(model_dict)

    start_epoch = checkpoint['epoch']
    best_top1 = checkpoint['best_top1']

    if Parallel:
        model = nn.DataParallel(model).cuda()

    return model, start_epoch, best_top1


def main(args):
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    cudnn.benchmark = True

    # Redirect print to both console and log file
    date_str = '{}'.format(datetime.datetime.today().strftime('%Y-%m-%d_%H-%M-%S'))
    if (not args.evaluate) and args.log:
        sys.stdout = Logger(
            osp.join(args.logs_dir, 'log_{}.txt'.format(date_str)))
        # save opts
        with open(osp.join(args.logs_dir, 'args_{}.json'.format(date_str)), 'w') as fp:
            json.dump(vars(args), fp, indent=1)

    # Create data loaders
    dataset, num_classes, train_loader, val_loader, query_loader, gallery_loader = \
        get_data(args.dataset, args.data_dir, args.height,
                 args.width, args.batch_size, args.num_workers,
                 args.combine_trainval, args.crop, args.mygt_icams, args.mygt_fps, args.re)

    # Create model
    model = models.create('pcb', num_features=args.features,
                          dropout=args.dropout, num_classes=num_classes, last_stride=args.last_stride,
                          output_feature=args.output_feature)

    # Load from checkpoint
    start_epoch = best_top1 = 0
    if args.resume:
        if args.evaluate:
            model, start_epoch, best_top1 = checkpoint_loader(model, args.resume, eval_only=True)
        else:
            model, start_epoch, best_top1 = checkpoint_loader(model, args.resume)
        print("=> Start epoch {}  best top1 {:.1%}".format(start_epoch, best_top1))
    model = nn.DataParallel(model).cuda()

    # Evaluator
    evaluator = Evaluator(model)
    if args.evaluate:
        print("Test:")
        evaluator.evaluate(query_loader, gallery_loader, dataset.query, dataset.gallery, eval_only=True)
        return

    # Criterion
    criterion = nn.CrossEntropyLoss().cuda()

    '''
    # step-1: train PCB
    '''
    if args.train_PCB:
        # Optimizer
        if hasattr(model.module, 'base'):  # low learning_rate the base network (aka. ResNet-50)
            base_param_ids = set(map(id, model.module.base.parameters()))
            new_params = [p for p in model.parameters() if
                          id(p) not in base_param_ids]
            param_groups = [
                {'params': model.module.base.parameters(), 'lr_mult': 0.1},
                {'params': new_params, 'lr_mult': 1.0}]
        else:
            param_groups = model.parameters()
        optimizer = torch.optim.SGD(param_groups, lr=args.lr,
                                    momentum=args.momentum,
                                    weight_decay=args.weight_decay,
                                    nesterov=True)

        # Trainer
        trainer = Trainer(model, criterion)

        # Schedule learning rate
        def adjust_lr(epoch):
            if args.epochs == 50:
                step_size = 30
            else:
                step_size = args.step_size
            lr = args.lr * (0.1 ** (epoch // step_size))
            for g in optimizer.param_groups:
                g['lr'] = lr * g.get('lr_mult', 1)

        # Draw Curve
        x_epoch = []
        fig = plt.figure()
        ax0 = fig.add_subplot(121, title="loss")
        ax1 = fig.add_subplot(122, title="prec")

        loss_s = []
        prec_s = []

        def draw_curve(current_epoch, train_loss, train_prec):
            x_epoch.append(current_epoch)
            ax0.plot(x_epoch, train_loss, 'bo-', label='train')
            ax1.plot(x_epoch, train_prec, 'bo-', label='train')
            if current_epoch == 0:
                ax0.legend()
                ax1.legend()
            fig.savefig(os.path.join(args.logs_dir, 'train_{}.jpg'.format(date_str)))

        # Start training
        for epoch in range(start_epoch, args.epochs):
            t0 = time.time()
            adjust_lr(epoch)
            # train_loss, train_prec = 0, 0
            train_loss, train_prec = trainer.train(epoch, train_loader, optimizer, fix_bn=args.fix_bn)

            if epoch < args.start_save:
                continue
            # skip evaluate
            top1 = 50

            is_best = top1 >= best_top1
            best_top1 = max(top1, best_top1)
            save_checkpoint({
                'state_dict': model.module.state_dict(),
                'epoch': epoch + 1,
                'best_top1': best_top1,
                'rpp': False,
            }, is_best, fpath=osp.join(args.logs_dir, 'checkpoint_epoch{}.pth.tar'.format(epoch)))
            loss_s.append(train_loss)
            prec_s.append(train_prec)
            draw_curve(epoch, loss_s, prec_s)

            t1 = time.time()
            t_epoch = t1 - t0
            print('\n * Finished epoch {:3d}  top1: {:5.1%}  best: {:5.1%}{}\n'.
                  format(epoch, top1, best_top1, ' *' if is_best else ''))
            print('*************** Epoch takes time: {:^10.2f} *********************\n'.format(t_epoch))

        # Final test
        print('Test with best model:')
        model, _, _ = checkpoint_loader(model, osp.join(args.logs_dir, 'model_best.pth.tar'), eval_only=True)

        evaluator.evaluate(query_loader, gallery_loader, dataset.query, dataset.gallery, eval_only=True)
        pass
    if args.train_RPP:
        '''
        step-2: add RPP
        '''
        model.module.enable_RPP()

        '''
        step-3: train the Refined pooling layer(weights)
        '''

        '''
        step-4: fine-tune the whole net
        '''
        # UnFreeze the whole model
        for param in model.module.parameters():
            param.requires_grad = True

        # Optimizer
        if hasattr(model.module, 'base'):  # low learning_rate the base network (aka. ResNet-50)
            base_param_ids = set(map(id, model.module.base.parameters()))
            new_params = [p for p in model.parameters() if
                          id(p) not in base_param_ids]
            param_groups = [
                {'params': model.module.base.parameters(), 'lr_mult': 0.1},
                {'params': new_params, 'lr_mult': 1.0}]
        else:
            param_groups = model.parameters()
        optimizer = torch.optim.SGD(param_groups, lr=args.lr,
                                    momentum=args.momentum,
                                    weight_decay=args.weight_decay,
                                    nesterov=True
                                    )

        # Trainer
        trainer = Trainer(model, criterion)

        # def adjust_lr(epoch):
        #     step_size = 60 if args.arch == 'inception' else 40
        #     lr = args.lr * (0.1 ** (epoch // step_size))
        #     for g in optimizer.param_groups:
        #         g['lr'] = lr * g.get('lr_mult', 1)
        def adjust_lr(epoch):
            lr = args.lr * 0.1
            for g in optimizer.param_groups:
                g['lr'] = lr * g.get('lr_mult', 1)

        if args.train_PCB:  # if have just trained pcb model in the same run
            start_epoch = epoch + 1
        best_top1 = 0  # save new models at logs/.../pcb_n_rpp/

        # Start training
        for epoch in range(start_epoch, args.epochs + 20):
            adjust_lr(epoch)
            trainer.train(epoch, train_loader, optimizer)
            if epoch < args.start_save:
                continue
            top1 = evaluator.evaluate(val_loader, dataset.val, dataset.val)

            is_best = top1 >= best_top1
            best_top1 = max(top1, best_top1)
            save_checkpoint({
                'state_dict': model.module.state_dict(),
                'epoch': epoch + 1,
                'best_top1': best_top1,
                'rpp': True,
            }, is_best, fpath=osp.join(args.logs_dir, 'checkpoint.pth.tar'))

            print('\n * Finished epoch {:3d}  top1: {:5.1%}  best: {:5.1%}{}\n'.
                  format(epoch, top1, best_top1, ' *' if is_best else ''))

        # Final test
        print('Test with best model:')
        model, start_epoch, best_top1 = checkpoint_loader(model, osp.join(args.logs_dir, 'model_best.pth.tar'),
                                                          eval_only=True)
        print("=> Start epoch {}  best top1 {:.1%}".format(start_epoch, best_top1))

        evaluator.evaluate(query_loader, gallery_loader, dataset.query, dataset.gallery, eval_only=True)
    pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Softmax loss classification")
    parser.add_argument('--log', type=str2bool, default=1)
    # data
    parser.add_argument('-d', '--dataset', type=str, default='market1501',
                        choices=datasets.names())
    parser.add_argument('-b', '--batch-size', type=int, default=64, help="batch size")
    parser.add_argument('-j', '--num-workers', type=int, default=8)
    parser.add_argument('--height', type=int, default=384,
                        help="input height, default: 384 for PCB*")
    parser.add_argument('--width', type=int, default=128,
                        help="input width, default: 128 for resnet*")
    parser.add_argument('--combine-trainval', action='store_true',
                        help="train and val sets together for training, "
                             "val set alone for validation")
    parser.add_argument('--mygt_icams', type=int, default=0, help="specify if train on single iCam")
    parser.add_argument('--mygt_fps', type=int, default=60,
                        choices=[1, 6, 12, 30, 60], help="specify if train on single iCam")
    parser.add_argument('--re', type=float, default=0, help="random erasing")
    # model
    parser.add_argument('--features', type=int, default=256)
    parser.add_argument('--dropout', type=float, default=0.5)
    parser.add_argument('-s', '--last_stride', type=int, default=1,
                        choices=[1, 2])
    parser.add_argument('--output_feature', type=str, default='fc',
                        choices=['pool5', 'fc'])
    # optimizer
    parser.add_argument('--lr', type=float, default=0.1,
                        help="learning rate of new parameters, for pretrained "
                             "parameters it is 10 times smaller than this")
    parser.add_argument('--momentum', type=float, default=0.9)
    parser.add_argument('--weight-decay', type=float, default=5e-4)
    # training configs
    parser.add_argument('--train-PCB', action='store_true',
                        help="train PCB model from start")
    parser.add_argument('--train-RPP', action='store_true',
                        help="train PCB model with RPP")
    parser.add_argument('--crop', action='store_true',
                        help="resize then crop, default: False")
    parser.add_argument('--fix_bn', type=str2bool, default=0,
                        help="fix (skip training) BN in base network")
    parser.add_argument('--resume', type=str, default='', metavar='PATH')
    parser.add_argument('--evaluate', action='store_true',
                        help="evaluation only")
    parser.add_argument('--epochs', type=int, default=60)
    parser.add_argument('--step-size',type=int, default=40)
    parser.add_argument('--start_save', type=int, default=0,
                        help="start saving checkpoints after specific epoch")
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--print-freq', type=int, default=1)
    # misc
    working_dir = osp.dirname(osp.abspath(__file__))
    parser.add_argument('--data-dir', type=str, metavar='PATH',
                        default=osp.join(working_dir, 'data'))
    parser.add_argument('--logs-dir', type=str, metavar='PATH',
                        default=osp.join(working_dir, 'logs'))
    main(parser.parse_args())