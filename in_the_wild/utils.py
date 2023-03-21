# Copyright (c) 2018-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#
import hashlib
import os
import pathlib
import shutil
import sys
import time

import cv2
import numpy as np
import torch
from common.arguments_diffusion import parse_args


def add_path():
    Alphapose_path = os.path.abspath('joints_detectors/Alphapose')
    hrnet_path = os.path.abspath('joints_detectors/hrnet')
    trackers_path = os.path.abspath('pose_trackers')
    paths = filter(lambda p: p not in sys.path, [Alphapose_path, hrnet_path, trackers_path])

    sys.path.extend(paths)


def wrap(func, *args, unsqueeze=False):
    """
    Wrap a torch function so it can be called with NumPy arrays.
    Input and return types are seamlessly converted.
    """

    # Convert input types where applicable
    args = list(args)
    for i, arg in enumerate(args):
        if type(arg) == np.ndarray:
            args[i] = torch.from_numpy(arg)
            if unsqueeze:
                args[i] = args[i].unsqueeze(0)

    result = func(*args)

    # Convert output types where applicable
    if isinstance(result, tuple):
        result = list(result)
        for i, res in enumerate(result):
            if type(res) == torch.Tensor:
                if unsqueeze:
                    res = res.squeeze(0)
                result[i] = res.numpy()
        return tuple(result)
    elif type(result) == torch.Tensor:
        if unsqueeze:
            result = result.squeeze(0)
        return result.numpy()
    else:
        return result


def deterministic_random(min_value, max_value, data):
    digest = hashlib.sha256(data.encode()).digest()
    raw_value = int.from_bytes(digest[:4], byteorder='little', signed=False)
    return int(raw_value / (2 ** 32 - 1) * (max_value - min_value)) + min_value


def alpha_map(prediction):
    p_min, p_max = prediction.min(), prediction.max()

    k = 1.6 / (p_max - p_min)
    b = 0.8 - k * p_max

    prediction = k * prediction + b

    return prediction


def change_score(prediction, detectron_detection_path):
    detectron_predictions = np.load(detectron_detection_path, allow_pickle=True)['positions_2d'].item()
    pose = detectron_predictions['S1']['Directions 1']
    prediction[..., 2] = pose[..., 2]

    return prediction


class Timer:
    def __init__(self, message, show=True):
        self.message = message
        self.elapsed = 0
        self.show = show

    def __enter__(self):
        self.start = time.perf_counter()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.show:
            print(f'{self.message} --- elapsed time: {time.perf_counter() - self.start} s')


def calculate_area(data):
    """
    Get the rectangle area of keypoints.
    :param data: AlphaPose json keypoint format([x, y, score, ... , x, y, score]) or AlphaPose result keypoint format([[x, y], ..., [x, y]])
    :return: area
    """
    data = np.array(data)

    if len(data.shape) == 1:
        data = np.reshape(data, (-1, 3))

    width = min(data[:, 0]) - max(data[:, 0])
    height = min(data[:, 1]) - max(data[:, 1])

    return np.abs(width * height)


def read_video(filename, fps=None, skip=0, limit=-1):
    stream = cv2.VideoCapture(filename)

    i = 0
    while True:
        grabbed, frame = stream.read()
        # if the `grabbed` boolean is `False`, then we have
        # reached the end of the video file
        if not grabbed:
            print('===========================> This video get ' + str(i) + ' frames in total.')
            sys.stdout.flush()
            break

        i += 1
        if i > skip:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            yield np.array(frame)
        if i == limit:
            break


def split_video(video_path):
    stream = cv2.VideoCapture(video_path)

    output_dir = os.path.dirname(video_path)
    video_name = os.path.basename(video_path)
    video_name = video_name[:video_name.rfind('.')]

    save_folder = pathlib.Path(f'./{output_dir}/alpha_pose_{video_name}/split_image/')
    shutil.rmtree(str(save_folder), ignore_errors=True)
    save_folder.mkdir(parents=True, exist_ok=True)

    total_frames = int(stream.get(cv2.CAP_PROP_FRAME_COUNT))
    length = len(str(total_frames)) + 1

    i = 1
    while True:
        grabbed, frame = stream.read()

        if not grabbed:
            print(f'Split totally {i + 1} images from video.')
            break

        save_path = f'{save_folder}/output{str(i).zfill(length)}.png'
        cv2.imwrite(save_path, frame)

        i += 1

    saved_path = os.path.dirname(save_path)
    print(f'Split images saved in {saved_path}')

    return saved_path


def evaluate(test_generator, model_pos, action=None, return_predictions=False):
    """
    Inference the 3d positions from 2d position.
    :type test_generator: UnchunkedGenerator
    :param test_generator:
    :param model_pos: 3d pose model
    :param return_predictions: return predictions if true
    :return:
    """
    joints_left, joints_right = list([4, 5, 6, 11, 12, 13]), list([1, 2, 3, 14, 15, 16])
    with torch.no_grad():
        model_pos.eval()
        N = 0
        for _, batch, batch_2d in test_generator.next_epoch():
            inputs_2d = torch.from_numpy(batch_2d.astype('float32'))
            if torch.cuda.is_available():
                inputs_2d = inputs_2d.cuda()
            # Positional model
            predicted_3d_pos = model_pos(inputs_2d)
            if test_generator.augment_enabled():
                # Undo flipping and take average with non-flipped version
                predicted_3d_pos[1, :, :, 0] *= -1
                predicted_3d_pos[1, :, joints_left + joints_right] = predicted_3d_pos[1, :, joints_right + joints_left]
                predicted_3d_pos = torch.mean(predicted_3d_pos, dim=0, keepdim=True)
            if return_predictions:
                return predicted_3d_pos.squeeze(0).cpu().numpy()

def eval_data_prepare(receptive_field, inputs_2d):
    # inputs_2d_p = torch.squeeze(inputs_2d)
    # inputs_3d_p = inputs_3d.permute(1,0,2,3)
    # out_num = inputs_2d_p.shape[0] - receptive_field + 1
    # eval_input_2d = torch.empty(out_num, receptive_field, inputs_2d_p.shape[1], inputs_2d_p.shape[2])
    # for i in range(out_num):
    #     eval_input_2d[i,:,:,:] = inputs_2d_p[i:i+receptive_field, :, :]
    # return eval_input_2d, inputs_3d_p
    ### split into (f/f1, f1, n, 2)
    #assert inputs_2d.shape[:-1] == inputs_3d.shape[:-1], "2d and 3d inputs shape must be same! "+str(inputs_2d.shape)+str(inputs_3d.shape)
    from einops import rearrange

    inputs_2d_p = torch.squeeze(inputs_2d)
    #inputs_3d_p = torch.squeeze(inputs_3d)

    if inputs_2d_p.shape[0] / receptive_field > inputs_2d_p.shape[0] // receptive_field:
        out_num = inputs_2d_p.shape[0] // receptive_field+1
    elif inputs_2d_p.shape[0] / receptive_field == inputs_2d_p.shape[0] // receptive_field:
        out_num = inputs_2d_p.shape[0] // receptive_field

    eval_input_2d = torch.empty(out_num, receptive_field, inputs_2d_p.shape[1], inputs_2d_p.shape[2])
    #eval_input_3d = torch.empty(out_num, receptive_field, inputs_3d_p.shape[1], inputs_3d_p.shape[2])

    for i in range(out_num-1):
        eval_input_2d[i,:,:,:] = inputs_2d_p[i*receptive_field:i*receptive_field+receptive_field,:,:]
        #eval_input_3d[i,:,:,:] = inputs_3d_p[i*receptive_field:i*receptive_field+receptive_field,:,:]
    if inputs_2d_p.shape[0] < receptive_field:
        from torch.nn import functional as F
        pad_right = receptive_field-inputs_2d_p.shape[0]
        inputs_2d_p = rearrange(inputs_2d_p, 'b f c -> f c b')
        inputs_2d_p = F.pad(inputs_2d_p, (0,pad_right), mode='replicate')
        # inputs_2d_p = np.pad(inputs_2d_p, ((0, receptive_field-inputs_2d_p.shape[0]), (0, 0), (0, 0)), 'edge')
        inputs_2d_p = rearrange(inputs_2d_p, 'f c b -> b f c')
    # if inputs_3d_p.shape[0] < receptive_field:
    #     pad_right = receptive_field-inputs_3d_p.shape[0]
    #     inputs_3d_p = rearrange(inputs_3d_p, 'b f c -> f c b')
    #     inputs_3d_p = F.pad(inputs_3d_p, (0,pad_right), mode='replicate')
    #     inputs_3d_p = rearrange(inputs_3d_p, 'f c b -> b f c')
    eval_input_2d[-1,:,:,:] = inputs_2d_p[-receptive_field:,:,:]
    #eval_input_3d[-1,:,:,:] = inputs_3d_p[-receptive_field:,:,:]

    return eval_input_2d

def evaluate_diffusion(test_generator, model_pos, action=None, return_predictions=False, receptive_field=243, bs=2):
    """
    Inference the 3d positions from 2d position.
    :type test_generator: UnchunkedGenerator
    :param test_generator:
    :param model_pos: 3d pose model
    :param return_predictions: return predictions if true
    :return:
    """
    joints_left, joints_right = list([4, 5, 6, 11, 12, 13]), list([1, 2, 3, 14, 15, 16])
    kps_left, kps_right = list([1, 3, 5, 7, 9, 11, 13, 15]), list([2, 4, 6, 8, 10, 12, 14, 16])
    with torch.no_grad():
        model_pos.eval()
        N = 0
        for _, batch, batch_2d in test_generator.next_epoch():
            inputs_2d = torch.from_numpy(batch_2d.astype('float32'))

            # Positional model

            inputs_2d_flip = inputs_2d.clone()
            inputs_2d_flip[:, :, :, 0] *= -1
            inputs_2d_flip[:, :, kps_left + kps_right, :] = inputs_2d_flip[:, :, kps_right + kps_left, :]

            inputs_2d = eval_data_prepare(receptive_field, inputs_2d)
            inputs_2d_flip = eval_data_prepare(receptive_field, inputs_2d_flip)

            if torch.cuda.is_available():
                inputs_2d = inputs_2d.cuda()
                inputs_2d_flip = inputs_2d_flip.cuda()

            total_batch = (inputs_2d.shape[0] + bs - 1) // bs

            for batch_cnt in range(total_batch):
                if (batch_cnt + 1) * bs > inputs_2d.shape[0]:
                    inputs_2d_single = inputs_2d[batch_cnt * bs:]
                    inputs_2d_flip_single = inputs_2d_flip[batch_cnt * bs:]
                    #inputs_3d_single = inputs_3d[batch_cnt * bs:]
                else:
                    inputs_2d_single = inputs_2d[batch_cnt * bs:(batch_cnt + 1) * bs]
                    inputs_2d_flip_single = inputs_2d_flip[batch_cnt * bs:(batch_cnt + 1) * bs]
                    #inputs_3d_single = inputs_3d[batch_cnt * bs:(batch_cnt + 1) * bs]

                predicted_3d_pos_single = model_pos(inputs_2d_single, None,
                                                     input_2d_flip=inputs_2d_flip_single)  # b, t, h, f, j, c
                predicted_3d_pos_single[:, :, :, :, 0] = 0

                #predicted_3d_pos = model_pos(inputs_2d)
                # if test_generator.augment_enabled():
                #     # Undo flipping and take average with non-flipped version
                #     predicted_3d_pos[1, :, :, 0] *= -1
                #     predicted_3d_pos[1, :, joints_left + joints_right] = predicted_3d_pos[1, :, joints_right + joints_left]
                #     predicted_3d_pos = torch.mean(predicted_3d_pos, dim=0, keepdim=True)
                if return_predictions:
                    if batch_cnt == 0:
                        out_all = predicted_3d_pos_single.cpu().numpy()
                    else:
                        out_all = np.concatenate((out_all, predicted_3d_pos_single.cpu().numpy()), axis=0)
                    #return predicted_3d_pos.squeeze(0).cpu().numpy()

            return out_all

if __name__ == '__main__':
    os.chdir('..')

    split_video('outputs/kobe.mp4')
