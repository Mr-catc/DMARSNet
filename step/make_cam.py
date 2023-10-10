import torch
from torch import multiprocessing, cuda
from torch.utils.data import DataLoader
import torch.nn.functional as F
from torch.backends import cudnn

import numpy as np
import importlib
import os

import voc12.dataloader
from misc import torchutils, imutils

cudnn.enabled = True

def _work(process_id, model, dataset, args):

    databin = dataset[process_id]
    n_gpus = torch.cuda.device_count()
    data_loader = DataLoader(databin, shuffle=False, num_workers=args.num_workers // n_gpus, pin_memory=True)

    with torch.no_grad(), cuda.device(process_id):

        model.cuda()

        for iter, pack in enumerate(data_loader):

            img_name = pack['name'][0]
            label = pack['label'][0]
            size = pack['size']

            strided_size = imutils.get_strided_size(size, 4)
            strided_up_size = imutils.get_strided_up_size(size, 16)

            outputs = [model(img[0].cuda(non_blocking=True))
                        for img in pack['img']]
            outputs1 = [outputs[i][0] for i in range(7)]
            outputs2 = [outputs[i][1] for i in range(7)]

            strided_cam1 = torch.sum(torch.stack(
                [F.interpolate(torch.unsqueeze(o, 0), strided_size, mode='bilinear', align_corners=False)[0] for o
                 in outputs1]), 0)

            strided_cam2 = torch.sum(torch.stack(
                [F.interpolate(torch.unsqueeze(o, 0), strided_size, mode='bilinear', align_corners=False)[0] for o
                 in outputs2]), 0)

            highres_cam1 = [F.interpolate(torch.unsqueeze(o, 1), strided_up_size,
                                         mode='bilinear', align_corners=False) for o in outputs1]
            highres_cam1 = torch.sum(torch.stack(highres_cam1, 0), 0)[:, 0, :size[0], :size[1]]

            highres_cam2 = [F.interpolate(torch.unsqueeze(o, 1), strided_up_size,
                                         mode='bilinear', align_corners=False) for o in outputs2]
            highres_cam2 = torch.sum(torch.stack(highres_cam2, 0), 0)[:, 0, :size[0], :size[1]]

            valid_cat = torch.nonzero(label)[:, 0]

            strided_cam1 = strided_cam1[valid_cat]
            strided_cam1 /= F.adaptive_max_pool2d(strided_cam1, (1, 1)) + 1e-5

            strided_cam2 = strided_cam2[valid_cat]
            strided_cam2 /= F.adaptive_max_pool2d(strided_cam2, (1, 1)) + 1e-5

            highres_cam1 = highres_cam1[valid_cat]
            highres_cam1 /= F.adaptive_max_pool2d(highres_cam1, (1, 1)) + 1e-5

            highres_cam2 = highres_cam2[valid_cat]
            highres_cam2 /= F.adaptive_max_pool2d(highres_cam2, (1, 1)) + 1e-5


            strided_cam_weight = strided_cam1 * args.weight + strided_cam2 * (1 - args.weight)
            highres_cam_weight = highres_cam1 * args.weight + highres_cam2 * (1 - args.weight)


            # save cams
            np.save(os.path.join(args.cam_out_dir, img_name + '.npy'),
                    {"keys": valid_cat, "cam": strided_cam_weight.cpu(), "high_res": highres_cam_weight.cpu().numpy()})

            if process_id == n_gpus - 1 and iter % (len(databin) // 20) == 0:
                print("%d " % ((5*iter+1)//(len(databin) // 20)), end='')


def run(args):
    model = getattr(importlib.import_module(args.arb_network), 'CAM')()
    model.load_state_dict(torch.load(args.arb_weights_name + '.pth'), strict=True)
    model.eval()

    n_gpus = torch.cuda.device_count()

    dataset = voc12.dataloader.VOC12ClassificationDatasetMSF(args.train_list,
                                                             voc12_root=args.voc12_root, scales=args.cam_scales)
    dataset = torchutils.split_dataset(dataset, n_gpus)

    print('[ ', end='')
    multiprocessing.spawn(_work, nprocs=n_gpus, args=(model, dataset, args), join=True)
    print(']')

    torch.cuda.empty_cache()