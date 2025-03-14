# Copyright (c) 2018-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# command:
# CUDA_VISIBLE_DEVICES=1,2 OMP_NUM_THREADS=8 torchrun --nproc_per_node=2 run_root2.py -k cpn_ft_h36m_dbb -f 243 -s 243 -l log/root -c checkpoint/root -m CSTE

import numpy as np

from common.arguments import parse_args
import torch

import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import os
import sys
import errno
import math
from einops import rearrange, repeat
from copy import deepcopy
from collections import defaultdict

from common.camera import *
import collections
from model.MixSTEs import *
from model.stcformer import STCFormer
from common.skeleton import *

from common.loss import *
from common.generators import ChunkedGenerator_Seq, UnchunkedGenerator_Seq
from common.chunk_dataset import *
from time import time
from common.utils import *
from common.logging import Logger
from model.load_model import load_model
# from model.PoseMamba import PoseMamba
from torch.utils.tensorboard import SummaryWriter
from datetime import datetime
from progress.bar import Bar
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader, DistributedSampler
import torch.distributed as dist
import torch.multiprocessing as mp
import wandb
#cudnn.benchmark = True       
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False

# import ptvsd
# ptvsd.enable_attach(address = ('192.168.210.130', 5678))
# print("ptvsd start")
# ptvsd.wait_for_attach()
# print("start debuging")
# joints_errs = []
args = parse_args()
os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu

def cleanup():
    """DDP 종료"""
    dist.destroy_process_group()

if args.evaluate != '':
    description = "Evaluate!"
elif args.evaluate == '':
    
    description = "Train!"
def main():

    dist.init_process_group("nccl")
    rank = dist.get_rank()
    
    # initial setting
    TIMESTAMP = "{0:%Y%m%dT%H-%M-%S/}".format(datetime.now())
    # tensorboard
    if rank == 0:
        if not args.nolog:
            writer = SummaryWriter(args.log+'_'+TIMESTAMP)
            writer.add_text('description', description)
            writer.add_text('command', 'python ' + ' '.join(sys.argv))
            # logging setting
            logfile = os.path.join(args.log+'_'+TIMESTAMP, 'logging.log')
            sys.stdout = Logger(logfile)
        print(description)
        print('python ' + ' '.join(sys.argv))
        print("CUDA Device Count: ", torch.cuda.device_count())
        print(args)
    world_num = torch.cuda.device_count()

    # if not assign checkpoint path, Save checkpoint file into log folder
    if args.checkpoint=='':
        args.checkpoint = args.log+'_'+TIMESTAMP
    try:
        # Create checkpoint directory if it does not exist
        os.makedirs('checkpoint/' + args.checkpoint)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise RuntimeError('Unable to create checkpoint directory:', args.checkpoint)

    # dataset loading
    if rank == 0:
        print('Loading dataset...')
    dataset_path = 'data/data_3d_' + args.dataset + '.npz'
    if args.dataset == 'h36m':
        from common.h36m_dataset import Human36mDataset
        dataset = Human36mDataset(dataset_path)
    elif args.dataset.startswith('humaneva'):
        from common.humaneva_dataset import HumanEvaDataset
        dataset = HumanEvaDataset(dataset_path)
    elif args.dataset.startswith('custom'):
        from common.custom_dataset import CustomDataset
        dataset = CustomDataset('data/data_2d_' + args.dataset + '_' + args.keypoints + '.npz')
    else:
        raise KeyError('Invalid dataset')
    if rank == 0:
        print('Preparing data...')
    for subject in dataset.subjects():
        for action in dataset[subject].keys():
            anim = dataset[subject][action]

            if 'positions' in anim:
                positions_3d = []
                for cam in anim['cameras']:
                    pos_3d = world_to_camera(anim['positions'], R=cam['orientation'], t=cam['translation'])
                    pos_3d[:, 1:] -= pos_3d[:, :1] # Remove global offset, but keep trajectory in first position
                    positions_3d.append(pos_3d)
                anim['positions_3d'] = positions_3d
    if rank == 0:
        print('Loading 2D detections...')
    keypoints = np.load('data/data_2d_' + args.dataset + '_' + args.keypoints + '.npz', allow_pickle=True)
    keypoints_metadata = keypoints['metadata'].item()
    keypoints_symmetry = keypoints_metadata['keypoints_symmetry']
    kps_left, kps_right = list(keypoints_symmetry[0]), list(keypoints_symmetry[1])
    joints_left, joints_right = list(dataset.skeleton().joints_left()), list(dataset.skeleton().joints_right())
    keypoints = keypoints['positions_2d'].item()

    ###################
    for subject in dataset.subjects():
        assert subject in keypoints, 'Subject {} is missing from the 2D detections dataset'.format(subject)
        for action in dataset[subject].keys():
            assert action in keypoints[subject], 'Action {} of subject {} is missing from the 2D detections dataset'.format(action, subject)
            if 'positions_3d' not in dataset[subject][action]:
                continue

            for cam_idx in range(len(keypoints[subject][action])):

                # We check for >= instead of == because some videos in H3.6M contain extra frames
                mocap_length = dataset[subject][action]['positions_3d'][cam_idx].shape[0]
                assert keypoints[subject][action][cam_idx].shape[0] >= mocap_length

                if keypoints[subject][action][cam_idx].shape[0] > mocap_length:
                    # Shorten sequence
                    keypoints[subject][action][cam_idx] = keypoints[subject][action][cam_idx][:mocap_length]

            assert len(keypoints[subject][action]) == len(dataset[subject][action]['positions_3d'])

    for subject in keypoints.keys():
        for action in keypoints[subject]:
            for cam_idx, kps in enumerate(keypoints[subject][action]):
                # Normalize camera frame
                cam = dataset.cameras()[subject][cam_idx]
                kps[..., :2] = normalize_screen_coordinates(kps[..., :2], w=cam['res_w'], h=cam['res_h'])
                keypoints[subject][action][cam_idx] = kps

    subjects_train = args.subjects_train.split(',')
    subjects_semi = [] if not args.subjects_unlabeled else args.subjects_unlabeled.split(',')
    if not args.render:
        subjects_test = args.subjects_test.split(',')
    else:
        subjects_test = [args.viz_subject]


    def fetch(subjects, action_filter=None, subset=1, parse_3d_poses=True):
        out_poses_3d = []
        out_poses_2d = []
        out_camera_params = []
        actions = []
        for subject in subjects:
            for action in keypoints[subject].keys():
                if action_filter is not None:
                    found = False
                    for a in action_filter:
                        if action.startswith(a):
                            found = True
                            break
                    if not found:
                        continue

                poses_2d = keypoints[subject][action]
                for i in range(len(poses_2d)): # Iterate across cameras
                    out_poses_2d.append(poses_2d[i])

                if subject in dataset.cameras():
                    cams = dataset.cameras()[subject]
                    assert len(cams) == len(poses_2d), 'Camera count mismatch'
                    for cam in cams:
                        if 'intrinsic' in cam:
                            out_camera_params.append(cam['intrinsic'])
                            action_name = action.split(' ')[0]
                            actions.append(action_name)

                if parse_3d_poses and 'positions_3d' in dataset[subject][action]:
                    poses_3d = dataset[subject][action]['positions_3d']
                    assert len(poses_3d) == len(poses_2d), 'Camera count mismatch'
                    for i in range(len(poses_3d)): # Iterate across cameras
                        out_poses_3d.append(poses_3d[i])

        if len(out_camera_params) == 0:
            out_camera_params = None
        if len(out_poses_3d) == 0:
            out_poses_3d = None

        stride = args.downsample
        if subset < 1:
            for i in range(len(out_poses_2d)):
                n_frames = int(round(len(out_poses_2d[i])//stride * subset)*stride)
                start = deterministic_random(0, len(out_poses_2d[i]) - n_frames + 1, str(len(out_poses_2d[i])))
                out_poses_2d[i] = out_poses_2d[i][start:start+n_frames:stride]
                if out_poses_3d is not None:
                    out_poses_3d[i] = out_poses_3d[i][start:start+n_frames:stride]
        elif stride > 1:
            # Downsample as requested
            for i in range(len(out_poses_2d)):
                out_poses_2d[i] = out_poses_2d[i][::stride]
                if out_poses_3d is not None:
                    out_poses_3d[i] = out_poses_3d[i][::stride]


        return out_camera_params, out_poses_3d, out_poses_2d, actions

    class AverageMeter(object):
        """Computes and stores the average and current value"""

        def __init__(self):
            self.reset()

        def reset(self):
            self.val = 0
            self.avg = 0
            self.sum = 0
            self.count = 0

        def update(self, val, n=1):
            self.val = val
            self.sum += val * n
            self.count += n
            self.avg = self.sum / self.count

    action_filter = None if args.actions == '*' else args.actions.split(',')
    if action_filter is not None:
        print('Selected actions:', action_filter)

    cameras_valid, poses_valid, poses_valid_2d, actions_valid = fetch(subjects_test, action_filter)

    # set receptive_field as number assigned
    receptive_field = args.number_of_frames
    if rank == 0:
        print('INFO: Receptive field: {} frames'.format(receptive_field))
    if not args.nolog and rank == 0:
        writer.add_text(args.log+'_'+TIMESTAMP + '/Receptive field', str(receptive_field))
    pad = (receptive_field -1) // 2 # Padding on each side
    min_loss = args.min_loss
    width = cam['res_w']
    height = cam['res_h']
    num_joints = keypoints_metadata['num_joints']

    #########################################PoseTransformer
    if args.model == 'MotionAGFormer':
        model_pos_train = load_model(args.model, args)
        model_pos = load_model(args.model, args)
    elif args.model == 'STCFormer':
        model_pos_train = STCFormer()
        model_pos = STCFormer()
    else:
        try:
            model_pos_train =  eval(args.model)(num_frame=receptive_field, num_joints=num_joints, in_chans=2, embed_dim_ratio=args.cs, depth=args.dep,
                num_heads=8, mlp_ratio=2., qkv_bias=True, qk_scale=None,drop_path_rate=0.1)

            model_pos =  eval(args.model)(num_frame=receptive_field, num_joints=num_joints, in_chans=2, embed_dim_ratio=args.cs, depth=args.dep,
                    num_heads=8, mlp_ratio=2., qkv_bias=True, qk_scale=None,drop_path_rate=0)
        except:
            raise Exception("Undefined model name")


    ################ load weight ########################
    # posetrans_checkpoint = torch.load('./checkpoint/pretrained_posetrans.bin', map_location=lambda storage, loc: storage)
    # posetrans_checkpoint = posetrans_checkpoint["model_pos"]
    # model_pos_train = load_pretrained_weights(model_pos_train, posetrans_checkpoint)

    #################
    causal_shift = 0
    model_params = 0
    for parameter in model_pos.parameters():
        model_params += parameter.numel()
    if rank == 0:
        print('INFO: Trainable parameter count:', model_params/1000000, 'Million')
    if not args.nolog and rank == 0:
        writer.add_text(args.log+'_'+TIMESTAMP + '/Trainable parameter count', str(model_params/1000000) + ' Million')

    wandb_id = args.wandb_id if args.wandb_id != '' else wandb.util.generate_id()
    # make model parallel
    if torch.cuda.is_available():
        torch.cuda.set_device(rank)
        model_pos = nn.DataParallel(model_pos)
        model_pos = model_pos.cuda()
        
        model_pos_train = model_pos_train.to(rank)
        model_pos_train = DDP(model_pos_train, device_ids=[rank])

    if args.resume or args.evaluate:
        chk_filename = os.path.join(args.checkpoint, args.resume if args.resume else args.evaluate)
        chk_filename = "checkpoint/" + chk_filename
        checkpoint = torch.load(chk_filename, map_location=lambda storage, loc: storage)
        # chk_filename = args.resume or args.evaluate
        model_pos_train.load_state_dict(checkpoint['model_pos'], strict=False)
        model_pos.load_state_dict(checkpoint['model_pos'], strict=False)
        wandb_id = checkpoint['wandb_id'] if 'wandb_id' in checkpoint else wandb_id
        min_loss = checkpoint['min_loss'] if 'min_loss' in checkpoint else min_loss
        min_root = 100
        if rank == 0:
            print('Loading checkpoint', chk_filename)
            print('This model was trained for {} epochs'.format(checkpoint['epoch']))
            print('Best validation loss so far:', min_loss)
            print('wandb_id:', wandb_id)
    if not args.nolog and rank == 0:
        
                

        wandb.init(id=wandb_id,
        name=args.checkpoint+'_'+TIMESTAMP,
        # set the wandb project where this run will be logged
        resume="allow",
        project="Trajectory-Aware",

        # track hyperparameters and run metadata
        config=args
        )

    test_generator = UnchunkedGenerator_Seq(cameras_valid, poses_valid, poses_valid_2d, actions_valid, 
                                        pad=pad, causal_shift=causal_shift, augment=False,
                                        kps_left=kps_left, kps_right=kps_right, joints_left=joints_left, joints_right=joints_right)
    if rank == 0:
        print('INFO: Testing on {} frames'.format(test_generator.num_frames()))
    if not args.nolog and rank == 0:
        writer.add_text(args.log+'_'+TIMESTAMP + '/Testing Frames', str(test_generator.num_frames()))

    def eval_data_prepare(receptive_field, inputs_2d, inputs_3d):
        # inputs_2d_p = torch.squeeze(inputs_2d)
        # inputs_3d_p = inputs_3d.permute(1,0,2,3)
        # out_num = inputs_2d_p.shape[0] - receptive_field + 1
        # eval_input_2d = torch.empty(out_num, receptive_field, inputs_2d_p.shape[1], inputs_2d_p.shape[2])
        # for i in range(out_num):
        #     eval_input_2d[i,:,:,:] = inputs_2d_p[i:i+receptive_field, :, :]
        # return eval_input_2d, inputs_3d_p
        ### split into (f/f1, f1, n, 2)
        assert inputs_2d.shape[:-1] == inputs_3d.shape[:-1], "2d and 3d inputs shape must be same! "+str(inputs_2d.shape)+str(inputs_3d.shape)
        inputs_2d_p = torch.squeeze(inputs_2d)
        inputs_3d_p = torch.squeeze(inputs_3d)

        if inputs_2d_p.shape[0] / receptive_field > inputs_2d_p.shape[0] // receptive_field: 
            out_num = inputs_2d_p.shape[0] // receptive_field+1
        elif inputs_2d_p.shape[0] / receptive_field == inputs_2d_p.shape[0] // receptive_field:
            out_num = inputs_2d_p.shape[0] // receptive_field

        eval_input_2d = torch.empty(out_num, receptive_field, inputs_2d_p.shape[1], inputs_2d_p.shape[2])
        eval_input_3d = torch.empty(out_num, receptive_field, inputs_3d_p.shape[1], inputs_3d_p.shape[2])

        for i in range(out_num-1):
            eval_input_2d[i,:,:,:] = inputs_2d_p[i*receptive_field:i*receptive_field+receptive_field,:,:]
            eval_input_3d[i,:,:,:] = inputs_3d_p[i*receptive_field:i*receptive_field+receptive_field,:,:]
        if inputs_2d_p.shape[0] < receptive_field:
            from torch.nn import functional as F
            pad_right = receptive_field-inputs_2d_p.shape[0]
            inputs_2d_p = rearrange(inputs_2d_p, 'b f c -> f c b')
            inputs_2d_p = F.pad(inputs_2d_p, (0,pad_right), mode='replicate')
            # inputs_2d_p = np.pad(inputs_2d_p, ((0, receptive_field-inputs_2d_p.shape[0]), (0, 0), (0, 0)), 'edge')
            inputs_2d_p = rearrange(inputs_2d_p, 'f c b -> b f c')
        if inputs_3d_p.shape[0] < receptive_field:
            pad_right = receptive_field-inputs_3d_p.shape[0]
            inputs_3d_p = rearrange(inputs_3d_p, 'b f c -> f c b')
            inputs_3d_p = F.pad(inputs_3d_p, (0,pad_right), mode='replicate')
            inputs_3d_p = rearrange(inputs_3d_p, 'f c b -> b f c')
        eval_input_2d[-1,:,:,:] = inputs_2d_p[-receptive_field:,:,:]
        eval_input_3d[-1,:,:,:] = inputs_3d_p[-receptive_field:,:,:]

        return eval_input_2d, eval_input_3d


    ###################

    # Training start
    if not args.evaluate:
        cameras_train, poses_train, poses_train_2d, _ = fetch(subjects_train, action_filter, subset=args.subset)

        lr = args.learning_rate
        optimizer = optim.AdamW(model_pos_train.parameters(), lr=lr, weight_decay=0.1)

        lr_decay = args.lr_decay
        losses_3d_train = []
        losses_3d_train_eval = []
        losses_3d_valid = []

        epoch = 0
        initial_momentum = 0.1
        final_momentum = 0.001

        # get training data
        train_dataset = ChunkedDataset_Seq(cameras_train, poses_train, poses_train_2d, args.number_of_frames, args.stride,
                                        pad=pad, causal_shift=causal_shift, shuffle=False, augment=args.data_augmentation,
                                        kps_left=kps_left, kps_right=kps_right, joints_left=joints_left, joints_right=joints_right)
        sampler = DistributedSampler(train_dataset, num_replicas=torch.cuda.device_count(),rank=rank, shuffle=True)
        dataloader = DataLoader(train_dataset,sampler=sampler, batch_size=args.batch_size, num_workers=8)
        train_generator_eval = UnchunkedGenerator_Seq(cameras_train, poses_train, poses_train_2d,
                                                pad=pad, causal_shift=causal_shift, augment=False)
        if rank == 0:
            print('INFO: Training on {} frames'.format(train_generator_eval.num_frames()))
        if not args.nolog and rank == 0:
            writer.add_text(args.log+'_'+TIMESTAMP + '/Training Frames', str(train_generator_eval.num_frames()))

        if args.resume:
            epoch = checkpoint['epoch']
            if 'optimizer' in checkpoint and checkpoint['optimizer'] is not None:
                # for name, param in checkpoint['model_pos'].items():
                #     print(f"Name: {name}, Shape: {param.shape}")
                optimizer.load_state_dict({
                    'state': checkpoint['optimizer']['state'],
                    'param_groups': optimizer.param_groups
                    })
            else:
                print('WARNING: this checkpoint does not contain an optimizer state. The optimizer will be reinitialized.')
            if not args.coverlr:
                lr = checkpoint['lr']
        if rank == 0:
            print('** Note: reported losses are averaged over all frames.')
            print('** The final evaluation will be carried out after the last training epoch.')

        # Pos model only
        while epoch < args.epochs:
            start_time = time()
            epoch_loss_3d_train = 0
            epoch_loss_traj_train = 0
            epoch_loss_2d_train_unlabeled = 0
            N = 0
            N_semi = 0
            model_pos_train.train()

            batch_time = AverageMeter()
            data_time = AverageMeter()
            mpj = AverageMeter()
            mpj_2d = AverageMeter()
            root = AverageMeter()
            end = time()


            # Just train 1 time, for quick debug
            notrain=False
            if rank == 0:
                bar = Bar('Train', max=len(dataloader))
            i = 0
            for cameras_train, inputs_3d, inputs_2d in dataloader:
                # if notrain:break
                # notrain=True
                # if cameras_train is not None:
                #     cameras_train = torch.from_numpy(cameras_train.astype('float32'))
                # inputs_3d = torch.from_numpy(batch_3d.astype('float32'))
                # inputs_2d = torch.from_numpy(batch_2d.astype('float32'))

                if torch.cuda.is_available():
                    inputs_3d = inputs_3d.cuda()
                    inputs_2d = inputs_2d.cuda()
                    if cameras_train is not None:
                        cameras_train = cameras_train.cuda()
                inputs_traj = inputs_3d[:, :, :1].clone()
                inputs_3d[:, :, 0] = 0
                optimizer.zero_grad()

                # Predict 3D poses
                predicted_3d_pos = model_pos_train(inputs_2d)

                gt2d = project_to_2d_linear(inputs_3d + inputs_traj, cameras_train)
                pred_traj, mask = get_root(predicted_3d_pos, inputs_2d, cameras_train)
                
                predicted_2d_pos = project_to_2d_linear(predicted_3d_pos + pred_traj, cameras_train)
                # predicted_3d_pos ,pred_traj, predicted_2d_pos = refine_pose(predicted_3d_pos, inputs_2d, cameras_train, iterations=3)


                # inputs_3d[:, :, :1] = inputs_traj
                # predicted_3d_pos[:, :, :1] = pred_traj


                # del inputs_2d
                # torch.cuda.empty_cache()
                ### weight mpjpe
                if args.dataset=='h36m':
                    # # hrdet
                    # w_mpjpe = torch.tensor([1, 1, 2.5, 2.5, 1, 2.5, 2.5, 1, 1, 1.5, 1.5, 4, 4, 1.5, 4, 4]).cuda()

                    w_mpjpe = torch.tensor([1, 1, 2.5, 2.5, 1, 2.5, 2.5, 1, 1, 1, 1.5, 1.5, 4, 4, 1.5, 4, 4]).cuda()
                
                elif args.dataset=='humaneva15':
                    w_mpjpe = torch.tensor([1, 1, 2.5, 2.5, 1, 2.5, 2.5, 1, 1.5, 1.5, 4, 4, 1.5, 4, 4]).cuda()

                b, f , _, _ = inputs_3d.shape
                loss_3d_pos = weighted_mpjpe(inputs_3d, predicted_3d_pos, w_mpjpe)
                # loss_3d_pos = mpjpe(inputs_3d.reshape(b,f, -1), predicted_3d_pos.reshape(b,f, -1))

                inputs_3d_masked = inputs_3d.clone()
                inputs_3d_masked[mask] = 0
                inputs_3d_masked[:, :, :1] = inputs_traj
                predicted_3d_pos_masked = predicted_3d_pos.clone()
                predicted_3d_pos_masked[mask] = 0
                predicted_3d_pos_masked[:, :, :1] = pred_traj
                gt2d_masked = gt2d.clone()
                gt2d_masked[mask] = 0
                predicted_2d_pos_masked = predicted_2d_pos.clone()
                predicted_2d_pos_masked[mask] = 0

                loss_3d_pos = weighted_mpjpe(inputs_3d_masked, predicted_3d_pos_masked, w_mpjpe)
                inputs_2d_error = mpjpe(gt2d, inputs_2d)
                loss_2d_pos = mpjpe(gt2d_masked, predicted_2d_pos_masked)
                pose_2d_error = mpjpe(gt2d, predicted_2d_pos)
                loss_root = mpjpe(inputs_traj, pred_traj)
                loss_resid = mpjpe(inputs_traj, pred_traj)
                loss_mpj = mpjpe(predicted_3d_pos - predicted_3d_pos[..., :1], inputs_3d - inputs_3d[..., :1])

                # Temporal Consistency Loss
                predicted_3d_pos[:, :, :1] = pred_traj
                inputs_3d[:, :, :1] = inputs_traj
                dif_seq = predicted_3d_pos[:,1:,:,:] - predicted_3d_pos[:,:-1,:,:]
                weights_joints = torch.ones_like(dif_seq).cuda()
                weights_mul = w_mpjpe
                assert weights_mul.shape[0] == weights_joints.shape[-2]
                weights_joints = torch.mul(weights_joints.permute(0,1,3,2),weights_mul).permute(0,1,3,2)
                dif_seq = torch.mean(torch.multiply(weights_joints, torch.square(dif_seq)))

                loss_diff = 0.5 * dif_seq + 2.0 * mean_velocity_error_train(predicted_3d_pos, inputs_3d, axis=1)
                

                loss_total = loss_3d_pos + loss_diff
                
                loss_total.backward(loss_total.clone().detach())

                loss_total = torch.mean(loss_total)

                epoch_loss_3d_train += inputs_3d.shape[0] * inputs_3d.shape[1] * loss_total.item()
                N += inputs_3d.shape[0] * inputs_3d.shape[1]

                optimizer.step()

                batch_time.update(time() - end)
                end = time()
                mpj.update(loss_mpj.item() * 1000, inputs_3d.shape[0])
                mpj_2d.update(pose_2d_error.item() * 1000 - inputs_2d_error.item() * 1000, inputs_3d.shape[0])
                root.update(loss_resid.item() * 1000, inputs_3d.shape[0])
                if rank == 0:
                    bar.suffix = '({batch}/{size}) Batch: {bt:.3f}s | Elapsed Time: {ttl:} | ETA: {eta:} ' \
                            '| MPJPE: {mpj: .1f}({loss: .1f}) | 2D Error:  {p2d: .1f}({input2d: .1f})| MRPE: {res: .1f}({root: .1f})' \
                    .format(batch=i + 1, size=len(dataloader), bt=batch_time.avg,
                            ttl=bar.elapsed_td, eta=bar.eta_td, loss=mpj.avg, mpj=loss_mpj.item() * 1000, p2d= pose_2d_error.item() * 1000,res=loss_resid.item() * 1000 ,input2d = mpj_2d.avg, root = root.avg)
                    bar.next()
                i += 1
            if rank == 0:
                bar.finish()
            losses_3d_train.append(epoch_loss_3d_train / N)
            torch.cuda.empty_cache()

            # End-of-epoch evaluation
            if rank == 0:
                valid_mpjpe = defaultdict(float)
                valid_vel = defaultdict(float)
                valid_root = defaultdict(float)
                num_frames = defaultdict(int)
                with torch.no_grad():
                    model_pos.load_state_dict(model_pos_train.state_dict(), strict=False)
                    model_pos.eval()

                    epoch_loss_3d_valid = 0
                    epoch_loss_traj_valid = 0
                    epoch_loss_2d_valid = 0
                    epoch_loss_3d_vel = 0
                    N = 0
                    if not args.no_eval:
                        # Evaluate on test set
                        for cam, batch, batch_2d, act_name in test_generator.next_epoch():
                            inputs_3d = torch.from_numpy(batch.astype('float32'))
                            inputs_2d = torch.from_numpy(batch_2d.astype('float32'))
                            cam = torch.from_numpy(cam.astype('float32'))

                            ##### apply test-time-augmentation (following Videopose3d)
                            inputs_2d_flip = inputs_2d.clone()
                            inputs_2d_flip[:, :, :, 0] *= -1
                            inputs_2d_flip[:, :, kps_left + kps_right, :] = inputs_2d_flip[:, :, kps_right + kps_left, :]

                            ##### convert size
                            inputs_3d_p = inputs_3d
                            inputs_2d, inputs_3d = eval_data_prepare(receptive_field, inputs_2d, inputs_3d_p)
                            inputs_2d_flip, _ = eval_data_prepare(receptive_field, inputs_2d_flip, inputs_3d_p)

                            if torch.cuda.is_available():
                                inputs_3d = inputs_3d.cuda()
                                inputs_2d = inputs_2d.cuda()
                                inputs_2d_flip = inputs_2d_flip.cuda()
                                b = inputs_3d.shape[0]
                                cam = cam.cuda()
                                cam = cam.repeat(b,1)
                            inputs_traj = inputs_3d[:, :, :1].clone()
                            inputs_3d[:, :, 0] = 0
                            predicted_3d_pos = model_pos(inputs_2d)
                            predicted_3d_pos_flip = model_pos(inputs_2d_flip)

                            predicted_3d_pos_flip[:, :, :, 0] *= -1
                            predicted_3d_pos_flip[:, :, joints_left + joints_right] = predicted_3d_pos_flip[:, :,
                                                                                    joints_right + joints_left]
                            for i in range(predicted_3d_pos.shape[0]):
                                # print(predicted_3d_pos[i,0,0,0], predicted_3d_pos_flip[i,0,0,0])
                                predicted_3d_pos[i,:,:,:] = (predicted_3d_pos[i,:,:,:] + predicted_3d_pos_flip[i,:,:,:])/2
                                # print(predicted_3d_pos[i,0,0,0], predicted_3d_pos_flip[i,0,0,0])
                            # predicted_3d_pos = torch.mean(torch.cat((predicted_3d_pos, predicted_3d_pos_flip), dim=1), dim=1, keepdim=True)
                            # predicted_3d_pos, pred_traj, predicted_2d_pos = refine_pose(predicted_3d_pos, inputs_2d, cam, iterations=3)
                            # del inputs_2d, inputs_2d_flip
                            # torch.cuda.empty_cache()
                            pred_root, _ = get_root(predicted_3d_pos, inputs_2d, cam)
                            # set root as zero
                            # predicted_3d_pos[:, :, 0] =
                            loss_3d_pos = mpjpe(predicted_3d_pos, inputs_3d)

                            valid_mpjpe[act_name] += inputs_3d.shape[0] * inputs_3d.shape[1] * loss_3d_pos.item()
                            valid_root[act_name] += inputs_3d.shape[0] * inputs_3d.shape[1] * mpjpe(pred_root, inputs_traj).item()
                            num_frames[act_name] += inputs_3d.shape[0] * inputs_3d.shape[1]

                            loss_3d_vel = mean_velocity_error_train(predicted_3d_pos, inputs_3d, axis=1)
                            valid_vel[act_name] += inputs_3d.shape[0] * inputs_3d.shape[1] * loss_3d_vel.item()


                            # del inputs_3d, loss_3d_pos, predicted_3d_pos
                            # torch.cuda.empty_cache()
                        for k in valid_mpjpe.keys():
                            valid_mpjpe[k] /= num_frames[k]
                            valid_vel[k] /= num_frames[k]
                            valid_root[k] /= num_frames[k]
                        losses_3d_valid = sum(valid_mpjpe.values()) / len(num_frames.keys())
                        epoch_loss_3d_vel = sum(valid_vel.values()) / len(num_frames.keys())
                        valid_root = sum(valid_root.values()) / len(num_frames.keys())

                        # Evaluate on training set, this time in evaluation mode
                        epoch_loss_3d_train_eval = 0
                        epoch_loss_traj_train_eval = 0
                        epoch_loss_2d_train_labeled_eval = 0
                        N = 0
                        for cam, batch, batch_2d, _ in train_generator_eval.next_epoch():
                            if batch_2d.shape[1] == 0:
                                # This can only happen when downsampling the dataset
                                continue

                            inputs_3d = torch.from_numpy(batch.astype('float32'))
                            inputs_2d = torch.from_numpy(batch_2d.astype('float32'))
                            cam = torch.from_numpy(cam.astype('float32'))
                            inputs_2d, inputs_3d = eval_data_prepare(receptive_field, inputs_2d, inputs_3d)

                            if torch.cuda.is_available():
                                inputs_3d = inputs_3d.cuda()
                                inputs_2d = inputs_2d.cuda()
                                b = inputs_3d.shape[0]
                                cam = cam.cuda()
                                cam = cam.repeat(b,1)
                            inputs_3d[:, :, 0] = 0

                            # Compute 3D poses
                            predicted_3d_pos = model_pos(inputs_2d)

                            # del inputs_2d
                            # torch.cuda.empty_cache()
                            
                            # set root as zero
                            # predicted_3d_pos[:, :, 0] = 0
                            loss_3d_pos = mpjpe(predicted_3d_pos, inputs_3d)
                            epoch_loss_3d_train_eval += inputs_3d.shape[0] * inputs_3d.shape[1] * loss_3d_pos.item()
                            N += inputs_3d.shape[0] * inputs_3d.shape[1]

                            # del inputs_3d, loss_3d_pos, predicted_3d_pos
                            # torch.cuda.empty_cache()

                        losses_3d_train_eval.append(epoch_loss_3d_train_eval / N)

                        # Evaluate 2D loss on unlabeled training set (in evaluation mode)
                        epoch_loss_2d_train_unlabeled_eval = 0
                        N_semi = 0
            elapsed = (time() - start_time) / 60
            if rank == 0:
                if args.no_eval:
                    print('[%d] time %.2f lr %f 3d_train %f' % (
                        epoch + 1,
                        elapsed,
                        lr,
                        losses_3d_train[-1] * 1000))
                else:
                    print(f"[{epoch + 1}] time {elapsed:.2f} lr {lr:.6f} "
                          f"Loss(Train) {losses_3d_train[-1] * 1000:.1f} "
                          f"MPJPE(Train) {losses_3d_train_eval[-1] * 1000:.1f}mm "
                          f"MPJPE(Test) {losses_3d_valid * 1000:.1f}mm "
                          f"MRPE(Test) {valid_root * 1000:.1f}mm "
                          f"MPJVE(Test){epoch_loss_3d_vel * 1000:.2f}mm")
                    if not args.nolog:
                        writer.add_scalar("Loss/3d training eval loss", losses_3d_train_eval[-1] * 1000, epoch+1)
                        writer.add_scalar("Loss/3d validation loss", losses_3d_valid * 1000, epoch+1)
                        wandb.log({"3d_train": losses_3d_train[-1] * 1000, "3d_train_eval": losses_3d_train_eval[-1] * 1000, "3d_valid": losses_3d_valid * 1000, "3d_val_velocity": epoch_loss_3d_vel * 1000}, epoch+1)
                if not args.nolog:
                    writer.add_scalar("Loss/3d training loss", losses_3d_train[-1] * 1000, epoch+1)
                    writer.add_scalar("Parameters/learing rate", lr, epoch+1)
                    writer.add_scalar('Parameters/training time per epoch', elapsed, epoch+1)
                    wandb.log({"3d_train": losses_3d_train[-1] * 1000}, epoch+1, commit=True)
            # Decay learning rate exponentially
            lr *= lr_decay
            for param_group in optimizer.param_groups:
                param_group['lr'] *= lr_decay
            epoch += 1

            # Decay BatchNorm momentum
            # momentum = initial_momentum * np.exp(-epoch/args.epochs * np.log(initial_momentum/final_momentum))
            # model_pos_train.set_bn_momentum(momentum)

            # Save checkpoint if necessary
            if epoch % args.checkpoint_frequency == 0 and rank == 0:
                chk_path = os.path.join(args.checkpoint, 'epoch_{}.bin'.format(epoch))
                chk_path = "checkpoint/" + chk_path
                print('Saving checkpoint to', chk_path)

                torch.save({
                    'epoch': epoch,
                    'lr': lr,
                    'optimizer': optimizer.state_dict(),
                    'model_pos': model_pos_train.state_dict(),
                    'min_loss': min_loss,
                    'wandb_id': wandb_id
                    # 'model_traj': model_traj_train.state_dict() if semi_supervised else None,
                    # 'random_state_semi': semi_generator.random_state() if semi_supervised else None,
                }, chk_path)

            #### save best checkpoint
            best_chk_path = os.path.join(args.checkpoint, 'best_epoch.bin'.format(epoch))
            best_chk_path = "checkpoint/" + best_chk_path
            # min_loss = 41.65
            if rank == 0:
                if losses_3d_valid * 1000 < min_loss:
                    min_loss = losses_3d_valid * 1000
                    print("save best checkpoint")
                    torch.save({
                        'epoch': epoch,
                        'lr': lr,
                        'optimizer': optimizer.state_dict(),
                        'model_pos': model_pos_train.state_dict(),
                        "min_loss": min_loss,
                        'wandb_id': wandb_id
                        # 'model_traj': model_traj_train.state_dict() if semi_supervised else None,
                        # 'random_state_semi': semi_generator.random_state() if semi_supervised else None,
                    }, best_chk_path)
                best_chk_path = os.path.join(args.checkpoint, 'best_epochR.bin'.format(epoch))
                best_chk_path = "checkpoint/" + best_chk_path
                if valid_root * 1000 < min_root:
                    min_root = valid_root * 1000
                    print("save best checkpoint")
                    torch.save({
                        'epoch': epoch,
                        'lr': lr,
                        'optimizer': optimizer.state_dict(),
                        'model_pos': model_pos_train.state_dict(),
                        "min_loss": min_loss,
                        'wandb_id': wandb_id
                        # 'model_traj': model_traj_train.state_dict() if semi_supervised else None,
                        # 'random_state_semi': semi_generator.random_state() if semi_supervised else None,
                    }, best_chk_path)

            # Save training curves after every epoch, as .png images (if requested)
            if args.export_training_curves and epoch > 3:
                if 'matplotlib' not in sys.modules:
                    import matplotlib
                    matplotlib.use('Agg')
                    import matplotlib.pyplot as plt

                plt.figure()
                epoch_x = np.arange(3, len(losses_3d_train)) + 1
                plt.plot(epoch_x, losses_3d_train[3:], '--', color='C0')
                plt.plot(epoch_x, losses_3d_train_eval[3:], color='C0')
                plt.plot(epoch_x, losses_3d_valid[3:], color='C1')
                plt.legend(['3d train', '3d train (eval)', '3d valid (eval)'])
                plt.ylabel('MPJPE (m)')
                plt.xlabel('Epoch')
                plt.xlim((3, epoch))
                plt.savefig(os.path.join(args.checkpoint, 'loss_3d.png'))

                plt.close('all')
    # Training end

    # Evaluate
    def evaluate(test_generator, action=None, return_predictions=False, use_trajectory_model=False, newmodel=None):
        epoch_loss_3d_pos = 0
        epoch_loss_3d_pos_procrustes = 0
        epoch_loss_3d_pos_scale = 0
        epoch_loss_3d_vel = 0
        with torch.no_grad():
            if newmodel is not None:
                print('Loading comparison model')
                model_eval = newmodel
                chk_file_path = '/mnt/data3/home/zjl/workspace/3dpose/PoseFormer/checkpoint/train_pf_00/epoch_60.bin'
                print('Loading evaluate checkpoint of comparison model', chk_file_path)
                checkpoint = torch.load(chk_file_path, map_location=lambda storage, loc: storage)
                model_eval.load_state_dict(checkpoint['model_pos'], strict=False)
                model_eval.eval()
            else:
                model_eval = model_pos
                if not use_trajectory_model:
                    # load best checkpoint
                    if args.evaluate == '':
                        chk_file_path = os.path.join("checkpoint/", args.checkpoint, 'best_epoch.bin')
                        print('Loading best checkpoint', chk_file_path)
                    elif args.evaluate != '':
                        chk_file_path = os.path.join("checkpoint/", args.checkpoint, args.evaluate)
                        print('Loading evaluate checkpoint', chk_file_path)
                    checkpoint = torch.load(chk_file_path, map_location=lambda storage, loc: storage)
                    print('This model was trained for {} epochs'.format(checkpoint['epoch']))
                    # model_pos_train.load_state_dict(checkpoint['model_pos'], strict=False)
                    model_eval.load_state_dict(checkpoint['model_pos'], strict=False)
                    model_eval.eval()
            # else:
                # model_traj.eval()
            N = 0
            for cam, batch, batch_2d in test_generator.next_epoch():
                inputs_2d = torch.from_numpy(batch_2d.astype('float32'))
                inputs_3d = torch.from_numpy(batch.astype('float32'))
                cam = torch.from_numpy(cam.astype('float32'))


                ##### apply test-time-augmentation (following Videopose3d)
                inputs_2d_flip = inputs_2d.clone()
                inputs_2d_flip [:, :, :, 0] *= -1
                inputs_2d_flip[:, :, kps_left + kps_right,:] = inputs_2d_flip[:, :, kps_right + kps_left,:]

                ##### convert size
                inputs_3d_p = inputs_3d
                if newmodel is not None:
                    def eval_data_prepare_pf(receptive_field, inputs_2d, inputs_3d):
                        inputs_2d_p = torch.squeeze(inputs_2d)
                        inputs_3d_p = inputs_3d.permute(1,0,2,3)
                        padding = int(receptive_field//2)
                        inputs_2d_p = rearrange(inputs_2d_p, 'b f c -> f c b')
                        inputs_2d_p = F.pad(inputs_2d_p, (padding,padding), mode='replicate')
                        inputs_2d_p = rearrange(inputs_2d_p, 'f c b -> b f c')
                        out_num = inputs_2d_p.shape[0] - receptive_field + 1
                        eval_input_2d = torch.empty(out_num, receptive_field, inputs_2d_p.shape[1], inputs_2d_p.shape[2])
                        for i in range(out_num):
                            eval_input_2d[i,:,:,:] = inputs_2d_p[i:i+receptive_field, :, :]
                        return eval_input_2d, inputs_3d_p
                    
                    inputs_2d, inputs_3d = eval_data_prepare_pf(81, inputs_2d, inputs_3d_p)
                    inputs_2d_flip, _ = eval_data_prepare_pf(81, inputs_2d_flip, inputs_3d_p)
                else:
                    inputs_2d, inputs_3d = eval_data_prepare(receptive_field, inputs_2d, inputs_3d_p)
                    inputs_2d_flip, _ = eval_data_prepare(receptive_field, inputs_2d_flip, inputs_3d_p)

                if torch.cuda.is_available():
                    inputs_2d = inputs_2d.cuda()
                    inputs_2d_flip = inputs_2d_flip.cuda()
                    inputs_3d = inputs_3d.cuda()
                    b = inputs_3d.shape[0]
                    cam = cam.cuda()
                    cam = cam.repeat(b,1)
                
                inputs_traj = inputs_3d[:, :, :1].clone()
                inputs_3d[:, :, 0] = 0
                
                predicted_3d_pos = model_eval(inputs_2d)
                predicted_3d_pos_flip = model_eval(inputs_2d_flip)
                predicted_3d_pos_flip[:, :, :, 0] *= -1
                predicted_3d_pos_flip[:, :, joints_left + joints_right] = predicted_3d_pos_flip[:, :,
                                                                        joints_right + joints_left]
                for i in range(predicted_3d_pos.shape[0]):
                    predicted_3d_pos[i,:,:,:] = (predicted_3d_pos[i,:,:,:] + predicted_3d_pos_flip[i,:,:,:])/2
                predicted_3d_pos[:, :, 0] = 0
                # predicted_3d_pos, pred_root, predicted_2d_pos = refine_pose(predicted_3d_pos, inputs_2d, cam)
                pred_root, _ = get_root(predicted_3d_pos, inputs_2d, cam)

                if return_predictions:
                    return predicted_3d_pos.squeeze().cpu().numpy()
                
    
                error = mpjpe(predicted_3d_pos, inputs_3d)

                epoch_loss_3d_pos_scale += inputs_3d.shape[0]*inputs_3d.shape[1] * mpjpe(pred_root, inputs_traj).item()

                epoch_loss_3d_pos += inputs_3d.shape[0]*inputs_3d.shape[1] * error.item()
                N += inputs_3d.shape[0] * inputs_3d.shape[1]

                inputs = inputs_3d.cpu().numpy().reshape(-1, inputs_3d.shape[-2], inputs_3d.shape[-1])
                predicted_3d_pos = predicted_3d_pos.cpu().numpy().reshape(-1, inputs_3d.shape[-2], inputs_3d.shape[-1])

                epoch_loss_3d_pos_procrustes += inputs_3d.shape[0]*inputs_3d.shape[1] * p_mpjpe(predicted_3d_pos, inputs)

                # Compute velocity error
                epoch_loss_3d_vel += inputs_3d.shape[0]*inputs_3d.shape[1] * mean_velocity_error(predicted_3d_pos, inputs)
        if action is None:
            print('----------')
        else:
            print('----'+action+'----')
        e1 = (epoch_loss_3d_pos / N)*1000
        e2 = (epoch_loss_3d_pos_procrustes / N)*1000
        e3 = (epoch_loss_3d_pos_scale / N)*1000
        ev = (epoch_loss_3d_vel / N)*1000
        print('Test time augmentation:', test_generator.augment_enabled())
        print('Protocol #1 Error (MPJPE):', e1, 'mm')
        print('Protocol #2 Error (MRPE):', e3, 'mm')
        print('Protocol #3 Error (P-MPJPE):', e2, 'mm')
        print('Velocity Error (MPJVE):', ev, 'mm')
        print('----------')

        return e1, e2, e3, ev


    if args.render:
        print('Rendering...')

        input_keypoints = keypoints[args.viz_subject][args.viz_action][args.viz_camera].copy()
        ground_truth = None
        if args.viz_subject in dataset.subjects() and args.viz_action in dataset[args.viz_subject]:
            if 'positions_3d' in dataset[args.viz_subject][args.viz_action]:
                ground_truth = dataset[args.viz_subject][args.viz_action]['positions_3d'][args.viz_camera].copy()
        if ground_truth is None:
            print('INFO: this action is unlabeled. Ground truth will not be rendered.')

        gen = UnchunkedGenerator_Seq(None, [ground_truth], [input_keypoints],
                                pad=pad, causal_shift=causal_shift, augment=args.test_time_augmentation,
                                kps_left=kps_left, kps_right=kps_right, joints_left=joints_left, joints_right=joints_right)
        prediction = evaluate(gen, return_predictions=True)
        if args.compare:
            from common.model_poseformer import PoseTransformer
            model_pf = PoseTransformer(num_frame=81, num_joints=17, in_chans=2, num_heads=8, mlp_ratio=2., qkv_bias=False, qk_scale=None,drop_path_rate=0.1)
            if torch.cuda.is_available():
                model_pf = nn.DataParallel(model_pf)
                model_pf = model_pf.cuda()
            prediction_pf = evaluate(gen, newmodel=model_pf, return_predictions=True)
            
            # ### reshape prediction_pf as ground truth
            # if ground_truth.shape[0] / receptive_field > ground_truth.shape[0] // receptive_field: 
            #     batch_num = (ground_truth.shape[0] // receptive_field) +1
            #     prediction_pf_2 = np.empty_like(ground_truth)
            #     for i in range(batch_num-1):
            #         prediction_pf_2[i*receptive_field:(i+1)*receptive_field,:,:] = prediction_pf[i,:,:,:]
            #     left_frames = ground_truth.shape[0] - (batch_num-1)*receptive_field
            #     prediction_pf_2[-left_frames:,:,:] = prediction_pf[-1,-left_frames:,:,:]
            #     prediction_pf = prediction_pf_2
            # elif ground_truth.shape[0] / receptive_field == ground_truth.shape[0] // receptive_field:
            #     prediction_pf.reshape(ground_truth.shape[0], 17, 3)

        # if model_traj is not None and ground_truth is None:
        #     prediction_traj = evaluate(gen, return_predictions=True, use_trajectory_model=True)
        #     prediction += prediction_traj
        ### reshape prediction as ground truth
        if ground_truth.shape[0] / receptive_field > ground_truth.shape[0] // receptive_field: 
            batch_num = (ground_truth.shape[0] // receptive_field) +1
            prediction2 = np.empty_like(ground_truth)
            for i in range(batch_num-1):
                prediction2[i*receptive_field:(i+1)*receptive_field,:,:] = prediction[i,:,:,:]
            left_frames = ground_truth.shape[0] - (batch_num-1)*receptive_field
            prediction2[-left_frames:,:,:] = prediction[-1,-left_frames:,:,:]
            prediction = prediction2
        elif ground_truth.shape[0] / receptive_field == ground_truth.shape[0] // receptive_field:
            prediction.reshape(ground_truth.shape[0], 17, 3)

        if args.viz_export is not None:
            print('Exporting joint positions to', args.viz_export)
            # Predictions are in camera space
            np.save(args.viz_export, prediction)

        if args.viz_output is not None:
            if ground_truth is not None:
                # Reapply trajectory
                trajectory = ground_truth[:, :1]
                ground_truth[:, 1:] += trajectory
                prediction += trajectory
                if args.compare:
                    prediction_pf += trajectory

            # Invert camera transformation
            cam = dataset.cameras()[args.viz_subject][args.viz_camera]
            if ground_truth is not None:
                if args.compare:
                    prediction_pf = camera_to_world(prediction_pf, R=cam['orientation'], t=cam['translation'])
                prediction = camera_to_world(prediction, R=cam['orientation'], t=cam['translation'])
                ground_truth = camera_to_world(ground_truth, R=cam['orientation'], t=cam['translation'])
            else:
                # If the ground truth is not available, take the camera extrinsic params from a random subject.
                # They are almost the same, and anyway, we only need this for visualization purposes.
                for subject in dataset.cameras():
                    if 'orientation' in dataset.cameras()[subject][args.viz_camera]:
                        rot = dataset.cameras()[subject][args.viz_camera]['orientation']
                        break
                if args.compare:
                    prediction_pf = camera_to_world(prediction_pf, R=rot, t=0)
                    prediction_pf[:, :, 2] -= np.min(prediction_pf[:, :, 2])
                prediction = camera_to_world(prediction, R=rot, t=0)
                # We don't have the trajectory, but at least we can rebase the height
                prediction[:, :, 2] -= np.min(prediction[:, :, 2])
            
            if args.compare:
                anim_output = {'PoseFormer': prediction_pf}
                anim_output['Ours'] = prediction
                # print(prediction_pf.shape, prediction.shape)
            else:
                anim_output = {'Reconstruction': prediction}
                # anim_output = {'Reconstruction': ground_truth + np.random.normal(loc=0.0, scale=0.1, size=[ground_truth.shape[0], 17, 3])}
            
            if ground_truth is not None and not args.viz_no_ground_truth:
                anim_output['Ground truth'] = ground_truth

            input_keypoints = image_coordinates(input_keypoints[..., :2], w=cam['res_w'], h=cam['res_h'])

            from common.visualization import render_animation
            render_animation(input_keypoints, keypoints_metadata, anim_output,
                            dataset.skeleton(), dataset.fps(), args.viz_bitrate, cam['azimuth'], args.viz_output,
                            limit=args.viz_limit, downsample=args.viz_downsample, size=args.viz_size,
                            input_video_path=args.viz_video, viewport=(cam['res_w'], cam['res_h']),
                            input_video_skip=args.viz_skip)

    else:
        if rank == 0:
            print('Evaluating...')
            all_actions = {}
            all_actions_by_subject = {}
            for subject in subjects_test:
                if subject not in all_actions_by_subject:
                    all_actions_by_subject[subject] = {}

                for action in dataset[subject].keys():
                    action_name = action.split(' ')[0]
                    if action_name not in all_actions:
                        all_actions[action_name] = []
                    if action_name not in all_actions_by_subject[subject]:
                        all_actions_by_subject[subject][action_name] = []
                    all_actions[action_name].append((subject, action))
                    all_actions_by_subject[subject][action_name].append((subject, action))

            def fetch_actions(actions):
                out_poses_3d = []
                out_poses_2d = []
                out_camera_params = []

                for subject, action in actions:
                    poses_2d = keypoints[subject][action]
                    for i in range(len(poses_2d)): # Iterate across cameras
                        out_poses_2d.append(poses_2d[i])

                    poses_3d = dataset[subject][action]['positions_3d']
                    assert len(poses_3d) == len(poses_2d), 'Camera count mismatch'
                    for i in range(len(poses_3d)): # Iterate across cameras
                        out_poses_3d.append(poses_3d[i])
                    
                    if subject in dataset.cameras():
                        cams = dataset.cameras()[subject]
                        assert len(cams) == len(poses_2d), 'Camera count mismatch'
                        for cam in cams:
                            if 'intrinsic' in cam:
                                out_camera_params.append(cam['intrinsic'])


                stride = args.downsample
                if stride > 1:
                    # Downsample as requested
                    for i in range(len(out_poses_2d)):
                        out_poses_2d[i] = out_poses_2d[i][::stride]
                        if out_poses_3d is not None:
                            out_poses_3d[i] = out_poses_3d[i][::stride]

                return out_camera_params ,out_poses_3d, out_poses_2d

            def run_evaluation(actions, action_filter=None):
                errors_p1 = []
                errors_p2 = []
                errors_p3 = []
                errors_vel = []
                # joints_errs_list=[]

                for action_key in actions.keys():
                    if action_filter is not None:
                        found = False
                        for a in action_filter:
                            if action_key.startswith(a):
                                found = True
                                break
                        if not found:
                            continue

                    cams_act ,poses_act, poses_2d_act = fetch_actions(actions[action_key])
                    gen = UnchunkedGenerator_Seq(cams_act, poses_act, poses_2d_act,
                                            pad=pad, causal_shift=causal_shift, augment=args.test_time_augmentation,
                                            kps_left=kps_left, kps_right=kps_right, joints_left=joints_left,
                                            joints_right=joints_right)
                    e1, e2, e3, ev = evaluate(gen, action_key)
                    
                    # joints_errs_list.append(joints_errs)

                    errors_p1.append(e1)
                    errors_p2.append(e2)
                    errors_p3.append(e3)
                    errors_vel.append(ev)
                if rank == 0:
                    print('Protocol #1   (MPJPE) action-wise average:', round(np.mean(errors_p1), 1), 'mm')
                    print('Protocol #2 (A-MPJPE) action-wise average:', round(np.mean(errors_p3), 1), 'mm')
                    print('Protocol #3 (P-MPJPE) action-wise average:', round(np.mean(errors_p2), 1), 'mm')
                    print('Velocity      (MPJVE) action-wise average:', round(np.mean(errors_vel), 2), 'mm')

                    if not args.nolog:
                        wandb.summary['MPJPE'] = round(np.mean(errors_p1), 1)
                        wandb.summary['A-MPJPE'] = round(np.mean(errors_p3), 1)
                        wandb.summary['P-MPJPE'] = round(np.mean(errors_p2), 1)
                        wandb.summary['MPJVE'] = round(np.mean(errors_vel), 2)

                # joints_errs_np = np.array(joints_errs_list).reshape(-1, 17)
                # joints_errs_np = np.mean(joints_errs_np, axis=0).reshape(-1)
                # with open('output/mpjpe_joints.csv', 'a+') as f:
                #     for i in joints_errs_np:
                #         f.write(str(i)+'\n')

            if not args.by_subject:
                run_evaluation(all_actions, action_filter)
            else:
                for subject in all_actions_by_subject.keys():
                    print('Evaluating on subject', subject)
                    run_evaluation(all_actions_by_subject[subject], action_filter)
                    print('')
    
    if not args.nolog and rank == 0:
        writer.close()
        wandb.finish()
    
    cleanup()

if __name__ == "__main__":
    main()

