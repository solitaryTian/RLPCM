#!/usr/bin/env python
# coding=utf-8
# Copyright 2023 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and

import argparse, traceback
import copy, time, pdb
import functools
import gc, cv2
import itertools
import json
import logging
import math
import os, traceback
import random
import shutil
from pathlib import Path
from typing import List, Union
from collections import defaultdict

from PIL import Image
import accelerate
import numpy as np
import torch
import torch.nn.functional as F
import torch.utils.checkpoint
import torchvision.transforms.functional as TF
import transformers
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import ProjectConfiguration, set_seed
from huggingface_hub import create_repo
from packaging import version
from peft import LoraConfig, get_peft_model, get_peft_model_state_dict
from torch.utils.data import default_collate
from torchvision import transforms
from tqdm.auto import tqdm
from transformers import AutoTokenizer, PretrainedConfig
import diffusers
from diffusers import (
    AutoencoderKL,
    DDIMScheduler,
    LCMScheduler,
    StableDiffusionXLPipeline,
    UNet2DConditionModel,
)
from scheduling_ddpm_modified import DDPMScheduler
from diffusers.optimization import get_scheduler
from diffusers.utils import check_min_version, is_wandb_available
from diffusers.utils.import_utils import is_xformers_available
from torch.utils.data import DataLoader, Dataset
from discriminator_sdxl import Discriminator
from torch.utils.data import Sampler, BatchSampler, SequentialSampler, RandomSampler
import pytorch_lightning as pl
from itertools import chain, repeat
from get_phased_weight import process_and_plot_data
from itertools import permutations
from DMD_loss import predict_noise, get_x0_from_noise, SDGuidance

MAX_SEQ_LENGTH = 77

if is_wandb_available():
    import wandb



class QLearning:
    """ Q-learning算法 """
    def __init__(self, n_state, epsilon, alpha, gamma, n_action=4):
        self.Q_table = np.zeros([n_state, n_action])  # 初始化Q(s,a)表格
        self.n_action = n_action  # 动作个数
        self.alpha = alpha  # 学习率
        self.gamma = gamma  # 折扣因子
        self.epsilon = epsilon  # epsilon-贪婪策略中的参数

    def take_action(self, state):  #选取下一步的操作
        if np.random.random() < self.epsilon:
            action = np.random.randint(self.n_action)
            mark = 'random'
        else:
            action = np.argmax(self.Q_table[state])
            mark = ''
        return action, mark

    def best_action(self, state):  # 用于打印策略
        Q_max = np.max(self.Q_table[state])
        a = [0 for _ in range(self.n_action)]
        for i in range(self.n_action):
            if self.Q_table[state, i] == Q_max:
                a[i] = 1
        return a

    def update(self, s0, a0, r, s1):
        td_error = r + self.gamma * self.Q_table[s1].max(
        ) - self.Q_table[s0, a0]
        self.Q_table[s0, a0] += self.alpha * td_error



def get_rank(lst):
    # 将列表中的元素与其索引配对
    indexed_lst = [(value, idx) for idx, value in enumerate(lst)]
    
    # 按值升序排序
    sorted_lst = sorted(indexed_lst, key=lambda x: x[0])
    
    # 创建一个排名列表
    rank = [0] * len(lst)
    
    # 分配排名
    for i, (_, idx) in enumerate(sorted_lst):
        rank[idx] = str(i)  # 将排名转换为字符串
    perms = list(permutations('0123'))
    mapping = {''.join(p): i+1 for i, p in enumerate(perms)}
    return mapping[''.join(rank)]


# Will error if the minimal version of diffusers is not installed. Remove at your own risks.
check_min_version("0.18.0.dev0")

logger = get_logger(__name__)
def _repeat_to_at_least(iterable, n):
    repeat_times = math.ceil(n / len(iterable))
    repeated = chain.from_iterable(repeat(iterable, repeat_times))
    return list(repeated)


class ComicDatasetBucket(Dataset):
    def __init__(self, file_path, prompt_embeds=None, pooled_prompt_embeds=None, max_size=(1024,1024), divisible=64, stride=16, min_dim=512, base_res=(1024,1024), max_ar_error=4, dim_limit=2048):

        self.base_res = base_res
        self.prompt_embeds = prompt_embeds
        self.pooled_prompt_embeds = pooled_prompt_embeds
        max_tokens = (max_size[0]/stride) * (max_size[1]/stride)
        self.get_resolution(file_path)  # 从 JSON 文件读取数据
        self.gen_buckets(min_dim, max_tokens, dim_limit, stride, divisible)
        self.assign_buckets(max_ar_error)
        self.gen_index_map()

    def get_resolution(self, file_path):
        # 如果缓存文件存在，直接加载
        file_arr_path = file_path.replace('.json', '.npy')
        file_arr_path_caption = file_path.replace('.json', '_caption.npy')
        if os.path.exists(file_arr_path) and os.path.exists(file_arr_path_caption):
            self.res_map = np.load(file_arr_path, allow_pickle=True).item()
            self.text_map = np.load(file_arr_path_caption, allow_pickle=True).item()
            return

        # 初始化存储字典
        self.res_map = {}
        self.text_map = {}

        # 从 JSON 文件读取数据
        with open(file_path, 'r') as f:
            data = json.load(f)

        print(f'总数据量为{len(data)}')


    # 判断 JSON 数据的类型
        if isinstance(data, dict):  # 字典形式
            for each_file_path, info in tqdm(data.items()):
                original_size = info["original_image_size"]  # [宽, 高]
                caption = info["caption"]  # 图片标签

                # 存储图像尺寸和标签
                self.res_map[each_file_path] = tuple(original_size)  # (宽, 高)
                self.text_map[each_file_path] = caption
        elif isinstance(data, list):  # 列表形式
            for item in tqdm(data):  # 直接遍历列表
                image_path = item["image_path"]  # 图片路径
                original_size = item["size"]  # [宽, 高]
                if 'caption' in item and item['caption'] != None:
                    caption = item["caption"]  # 图片标签
                elif 'wd_tag' in item and item['wd_tag'] != None:
                    caption = item["wd_tag"]
                else:
                    caption = ''

                
                # 存储图像尺寸和标签
                self.res_map[image_path] = tuple(original_size)  # (宽, 高)
                self.text_map[image_path] = caption
        else:
            raise ValueError("Unsupported JSON format. Expected a dictionary or a list.")



        # 保存缓存文件
        np.save(file_path.replace('.json', '.npy'), np.array(self.res_map))
        np.save(file_path.replace('.json', '_caption.npy'), np.array(self.text_map))

    def gen_buckets(self, min_dim, max_tokens, dim_limit, stride=8, div=64):
        resolutions = []
        aspects = []
        w = min_dim
        while (w/stride) * (min_dim/stride) <= max_tokens and w <= dim_limit:
            h = min_dim
            got_base = False
            while (w/stride) * ((h+div)/stride) <= max_tokens and (h+div) <= dim_limit:
                if w == self.base_res[0] and h == self.base_res[1]:
                    got_base = True
                h += div
            if (w != self.base_res[0] or h != self.base_res[1]) and got_base:
                resolutions.append(self.base_res)
                aspects.append(1)
            resolutions.append((w, h))
            aspects.append(float(w)/float(h))
            w += div
        h = min_dim
        while (h/stride) * (min_dim/stride) <= max_tokens and h <= dim_limit:
            w = min_dim
            got_base = False
            while (h/stride) * ((w+div)/stride) <= max_tokens and (w+div) <= dim_limit:
                if w == self.base_res[0] and h == self.base_res[1]:
                    got_base = True
                w += div
            resolutions.append((w, h))
            aspects.append(float(w)/float(h))
            h += div

        res_map = {}
        for i, res in enumerate(resolutions):
            res_map[res] = aspects[i]

        self.resolutions = sorted(res_map.keys(), key=lambda x: x[0] * 4096 - x[1])
        self.aspects = np.array(list(map(lambda x: res_map[x], self.resolutions)))
        self.resolutions = np.array(self.resolutions)

    def assign_buckets(self, max_ar_error=4):
        self.buckets = {}
        self.aspect_errors = []
        self.res_map_new = {}

        skipped = 0
        skip_list = []
        for post_id in self.res_map.keys():
            w, h = self.res_map[post_id]
            aspect = float(w)/float(h)
            bucket_id = np.abs(self.aspects - aspect).argmin()
            if bucket_id not in self.buckets:
                self.buckets[bucket_id] = []
            error = abs(self.aspects[bucket_id] - aspect)
            if error < max_ar_error:
                self.buckets[bucket_id].append(post_id)
                self.res_map_new[post_id] = tuple(self.resolutions[bucket_id])
            else:
                skipped += 1
                skip_list.append(post_id)
        for post_id in skip_list:
            del self.res_map[post_id]

    def gen_index_map(self):
        self.id2path = {}
        self.id2shape = {}
        id = 0
        for path, shape in self.res_map_new.items():
            self.id2path[id] = path
            self.id2shape[id] = shape
            id += 1

    def __len__(self):
        return len(self.res_map)


    def __getitem__(self, idx):
        while True:
            try:
                target_path = self.id2path[idx]
                W, H = self.res_map_new[target_path]
                text = self.text_map[target_path]
                target_path = target_path.strip()

                # 加载图像
                target = cv2.imread(target_path)
                if target is None:
                    raise ValueError(f"Unable to read image at path: {target_path}")
                
                ori_H, ori_W, _ = target.shape

                target = cv2.cvtColor(target, cv2.COLOR_BGR2RGB)
                target = cv2.resize(target, (W, H))
                target = target.transpose((2,0,1))

                # 归一化到 [-1, 1]
                target = (target.astype(np.float32) / 127.5) - 1.0

                return dict(pixel_values=target, original_sizes=(ori_W, ori_H), crop_top_lefts=(0, 0), target_sizes=(W, H), caption=text)
            
            except Exception as e:
                traceback.print_exc()
                print(f"Skipping sample {idx} due to error: {e}, path: {target_path}")
                
                # 从当前桶中重新选择一个样本
                bucket_id = self.get_bucket_id(idx)
                if bucket_id is not None:
                    new_idx = random.choice(self.buckets[bucket_id])
                    idx = new_idx
                else:
                    idx = random.randint(0, len(self.id2path) - 1)  # 如果没有找到桶，随机选择一个样本
        


    def get_bucket_id(self, idx):
        target_path = self.id2path[idx]
        W, H = self.res_map_new[target_path]
        aspect = float(W) / float(H)
        bucket_id = np.abs(self.aspects - aspect).argmin()
        return bucket_id if bucket_id in self.buckets else None

    # def __getitem__(self, idx):
    #     while True:
    #         try:
    #             target_path = self.id2path[idx]
    #             W, H = self.res_map_new[target_path]
    #             text = self.text_map[target_path]
    #             target_path = target_path.strip()

    #             # 加载图像
    #             target = cv2.imread(target_path)
    #             if target is None:
    #                 raise ValueError(f"Unable to read image at path: {target_path}")
                
    #             ori_H, ori_W, _ = target.shape

    #             target = cv2.cvtColor(target, cv2.COLOR_BGR2RGB)
    #             target = cv2.resize(target, (W, H))
    #             target = target.transpose((2,0,1))

    #             # 归一化到 [-1, 1]
    #             target = (target.astype(np.float32) / 127.5) - 1.0

    #             return dict(pixel_values=target, original_sizes=(ori_W, ori_H), crop_top_lefts=(0, 0), target_sizes=(W, H), caption=text)
            
    #         except Exception as e:
    #             traceback.print_exc()
    #             print(f"Skipping sample {idx} due to error: {e}, path: {target_path}")
    #             idx = random.randint(0, len(self.id2path) - 1)  # 随机选择一个样本




    # def __getitem__(self, idx):
    #     target_path = self.id2path[idx]
    #     W, H = self.res_map_new[target_path]
    #     text = self.text_map[target_path]
    #     target_path = target_path.strip()

    #     # 加载图像
    #     try:
    #         target = cv2.imread(target_path)
    #         ori_H, ori_W, _ = target.shape
    #     except Exception as e:
    #         traceback.print_exc()
    #         print(f"Skipping sample {idx} due to error: {e},path:{target_path}")
    #         return None  # 返回 None 表示该样本有问题

    #     target = cv2.cvtColor(target, cv2.COLOR_BGR2RGB)
    #     target = cv2.resize(target, (W, H))
    #     target = target.transpose((2,0,1))

    #     # 归一化到 [-1, 1]
    #     target = (target.astype(np.float32) / 127.5) - 1.0

    #     return dict(pixel_values=target, original_sizes=(ori_W, ori_H), crop_top_lefts=(0, 0), target_sizes=(W, H), caption=text)



class GroupedBatchSampler(BatchSampler):
    # def __init__(self, sampler, batch_size, drop_last=True):
    def __init__(self, sampler, dataset, batch_size, drop_last=True):
        if not isinstance(sampler, Sampler):
            raise ValueError(f"sampler should be an instance of torch.utils.data.Sampler, but got sampler={sampler}")
        self.sampler = sampler
        # self.group_ids = self.sampler.dataset.id2shape
        self.group_ids = dataset.id2shape
        self.batch_size = batch_size
        self.drop_last = drop_last

    def __iter__(self):
        buffer_per_group = defaultdict(list)
        samples_per_group = defaultdict(list)

        num_batches = 0
        for idx in self.sampler:
            group_id = self.group_ids[idx]
            buffer_per_group[group_id].append(idx)
            samples_per_group[group_id].append(idx)
            if len(buffer_per_group[group_id]) == self.batch_size:
                yield buffer_per_group[group_id]
                num_batches += 1
                del buffer_per_group[group_id]
            assert len(buffer_per_group[group_id]) < self.batch_size

        expected_num_batches = len(self)
        num_remaining = expected_num_batches - num_batches
        if num_remaining > 0:
            for group_id, _ in sorted(buffer_per_group.items(), key=lambda x: len(x[1]), reverse=True):
                remaining = self.batch_size - len(buffer_per_group[group_id])
                samples_from_group_id = _repeat_to_at_least(samples_per_group[group_id], remaining)
                buffer_per_group[group_id].extend(samples_from_group_id[:remaining])
                assert len(buffer_per_group[group_id]) == self.batch_size
                yield buffer_per_group[group_id]
                num_remaining -= 1
                if num_remaining == 0:
                    break
        assert num_remaining == 0

    def __len__(self):
        return len(self.sampler) // self.batch_size
    
# using LightningDataModule
class ComicDataModule(pl.LightningDataModule):
    def __init__(self, batch_size, file_txt, prompt_embeds=None, pooled_prompt_embeds=None):
        super().__init__()
        self.save_hyperparameters()
        self.batch_size = batch_size
        # self.dataset = dataset
        self.file_txt = file_txt
        self.prompt_embeds = prompt_embeds
        self.pooled_prompt_embeds = pooled_prompt_embeds
    
    def setup(self, stage):
        self.dataset = ComicDatasetBucket(file_path=self.file_txt, prompt_embeds=self.prompt_embeds, pooled_prompt_embeds=self.pooled_prompt_embeds)
        self.sampler = SequentialSampler(self.dataset)

    def __len__(self):
        return len(self.dataset.res_map)


    def train_dataloader(self):
        def collate_fn(examples):
            examples = [sample for sample in examples if sample is not None]
            pixel_values = torch.stack([torch.tensor(example["pixel_values"]) for example in examples])
            pixel_values = pixel_values.to(memory_format=torch.contiguous_format).float()
            original_sizes = [example["original_sizes"] for example in examples]
            crop_top_lefts = [example["crop_top_lefts"] for example in examples]
            target_sizes = [example["target_sizes"] for example in examples]
            caption = [example["caption"] for example in examples]
            # prompt_embeds = torch.stack([torch.tensor(example["prompt_embeds"]) for example in examples])
            # pooled_prompt_embeds = torch.stack([torch.tensor(example["pooled_prompt_embeds"]) for example in examples])

            return {
                "pixel_values": pixel_values,
                # "prompt_embeds": prompt_embeds,
                # "pooled_prompt_embeds": pooled_prompt_embeds,
                "original_sizes": original_sizes,
                "crop_top_lefts": crop_top_lefts,
                "target_sizes": target_sizes,
                "caption": caption
            }
        # return DataLoader(self.dataset, batch_sampler=GroupedBatchSampler(sampler=self.sampler, batch_size=self.batch_size), num_workers=32, collate_fn=collate_fn)
        return DataLoader(self.dataset, batch_sampler=GroupedBatchSampler(sampler=self.sampler, dataset=self.dataset, batch_size=self.batch_size), num_workers=0, collate_fn=collate_fn, pin_memory=True, persistent_workers=False)





@torch.no_grad()
def update_ema(target_params, source_params, rate=0.99):
    """
    Update target parameters to be closer to those of source parameters using
    an exponential moving average.

    :param target_params: the target parameter sequence.
    :param source_params: the source parameter sequence.
    :param rate: the EMA rate (closer to 1 means slower).
    """
    for targ, src in zip(target_params, source_params):
        targ.detach().mul_(rate).add_(src, alpha=1 - rate)



def get_module_kohya_state_dict(
    module, prefix: str, dtype: torch.dtype, adapter_name: str = "default"
):
    kohya_ss_state_dict = {}
    for peft_key, weight in get_peft_model_state_dict(
        module, adapter_name=adapter_name
    ).items():
        kohya_key = peft_key.replace("base_model.model", prefix)
        kohya_key = kohya_key.replace("lora_A", "lora_down")
        kohya_key = kohya_key.replace("lora_B", "lora_up")
        kohya_key = kohya_key.replace(".", "_", kohya_key.count(".") - 2)
        kohya_ss_state_dict[kohya_key] = weight.to(dtype)

        # Set alpha parameter
        if "lora_down" in kohya_key:
            alpha_key = f'{kohya_key.split(".")[0]}.alpha'
            kohya_ss_state_dict[alpha_key] = torch.tensor(
                module.peft_config[adapter_name].lora_alpha
            ).to(dtype)

    return kohya_ss_state_dict


class CustomImageDataset_without_crop(Dataset):
    def __init__(self, img_dir, sample_size):
        """
        Args:
            img_dir (string): Directory with all the images and text files.
            sample_size (tuple): Desired sample size as (height, width).
        """
        self.img_dir = img_dir
        self.sample_size = sample_size
        self.img_names = [
            f for f in os.listdir(img_dir) if f.endswith((".png", ".jpg", '.webp'))
        ]
        self.transform = transforms.Compose(
            [
                transforms.Resize(
                    self.sample_size, interpolation=transforms.InterpolationMode.LANCZOS
                ),
                # transforms.CenterCrop(self.sample_size),
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            ]
        )

    def __len__(self):
        return len(self.img_names)

    def __getitem__(self, idx):

        while True:
            try:
                img_name = self.img_names[idx]
                img_path = os.path.join(self.img_dir, img_name)
                image = Image.open(img_path).convert("RGB")
                image = TF.resize(
                    image,
                    self.sample_size,
                    interpolation=transforms.InterpolationMode.LANCZOS,
                )
                # get crop coordinates and crop image
                # c_top, c_left, _, _ = transforms.RandomCrop.get_params(
                #     image, output_size=(self.sample_size, self.sample_size)
                # )
                # image = TF.crop(
                #     image, c_top, c_left, self.sample_size, self.sample_size
                # )
                image = TF.to_tensor(image)
                image = TF.normalize(image, [0.5], [0.5])
                text_name = img_name.rsplit(".", 1)[0] + ".txt"
                text_path = os.path.join(self.img_dir, text_name)
                with open(text_path, "r") as f:
                    text = f.read().strip()
                return (
                    image,
                    text,
                    (self.sample_size, self.sample_size),
                    # (c_top, c_left),
                    (0,0),
                )
            except:
                idx = np.random.randint(len(self.img_names))
                print("error")
                continue

class CustomImageDataset(Dataset):
    def __init__(self, img_dir, sample_size):
        """
        Args:
            img_dir (string): Directory with all the images and text files.
            sample_size (tuple): Desired sample size as (height, width).
        """
        self.img_dir = img_dir
        self.sample_size = sample_size
        self.img_names = [
            f for f in os.listdir(img_dir) if f.endswith((".png", ".jpg", '.webp'))
        ]
        self.transform = transforms.Compose(
            [
                transforms.Resize(
                    self.sample_size, interpolation=transforms.InterpolationMode.LANCZOS
                ),
                transforms.CenterCrop(self.sample_size),
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            ]
        )

    def __len__(self):
        return len(self.img_names)

    def __getitem__(self, idx):

        while True:
            try:
                img_name = self.img_names[idx]
                img_path = os.path.join(self.img_dir, img_name)
                image = Image.open(img_path).convert("RGB")
                image = TF.resize(
                    image,
                    self.sample_size,
                    interpolation=transforms.InterpolationMode.LANCZOS,
                )
                # get crop coordinates and crop image
                c_top, c_left, _, _ = transforms.RandomCrop.get_params(
                    image, output_size=(self.sample_size, self.sample_size)
                )
                image = TF.crop(
                    image, c_top, c_left, self.sample_size, self.sample_size
                )
                image = TF.to_tensor(image)
                image = TF.normalize(image, [0.5], [0.5])
                text_name = img_name.rsplit(".", 1)[0] + ".txt"
                text_path = os.path.join(self.img_dir, text_name)
                with open(text_path, "r") as f:
                    text = f.read().strip()
                return (
                    image,
                    text,
                    (self.sample_size, self.sample_size),
                    (c_top, c_left),
                    # (0,0),
                )
            except:
                idx = np.random.randint(len(self.img_names))
                print("error")
                continue


def generate_custom_random_numbers(batch_size, probs=[1/4, 1/4, 1/4, 1/4]):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    # 定义区间和对应的概率
    segments = [(0, 9), (10, 19), (20, 29), (30, 39)]

    # 选择区间
    segment_indices = torch.multinomial(
        torch.tensor(probs, device=device), 
        batch_size, 
        replacement=True
    )

    # 在每个区间内生成均匀随机数
    result = torch.zeros(batch_size, dtype=torch.long, device=device)
    for i, seg_idx in enumerate(segment_indices):
        try:
            start, end = segments[seg_idx]
        except:
            traceback.print_exc()
            import pdb; pdb.set_trace()

        result[i] = torch.randint(
            start, end + 1, (1,), device=device
        ).item()

    return result




@torch.no_grad()
def log_validation(
    vae, unet, args, accelerator, weight_dtype, step, inference_steps, cfg
):
    logger.info("Running validation... ")

    unet = accelerator.unwrap_model(unet)
    pipeline = StableDiffusionXLPipeline.from_pretrained(
        args.pretrained_teacher_model,
        vae=vae,
        scheduler=DDIMScheduler(
            num_train_timesteps=1000,
            beta_start=0.00085,
            beta_end=0.012,
            beta_schedule="scaled_linear",
            timestep_spacing="trailing",
            clip_sample=False,  # important. DDIM will apply True as default which causes inference degradation.
            set_alpha_to_one=False,
        ),  # DDIM should just work well. See our discussion on parameterization in the paper.
        revision=args.revision,
        torch_dtype=weight_dtype,
    )

    pipeline.set_progress_bar_config(disable=True)
    pipeline = pipeline.to(accelerator.device)

    lora_state_dict = get_module_kohya_state_dict(unet, "lora_unet", weight_dtype)
    pipeline.load_lora_weights(lora_state_dict)
    pipeline.fuse_lora()

    if args.enable_xformers_memory_efficient_attention:
        pipeline.enable_xformers_memory_efficient_attention()

    pipeline.enable_vae_slicing()

    if args.seed is None:
        generator = None
    else:
        generator = torch.Generator(device=accelerator.device).manual_seed(args.seed)

    validation_prompts = [
        "portrait photo of a girl, photograph, highly detailed face, depth of field, moody light, golden hour, style by Dan Winters, Russell James, Steve McCurry, centered, extremely detailed, Nikon D850, award winning photography",
        # "Self-portrait oil painting, a beautiful cyborg with golden hair, 8k",
        # "Astronaut in a jungle, cold color palette, muted colors, detailed, 8k",
        # "A photo of beautiful mountain with realistic sunset and blue lake, highly detailed, masterpiece",
    ]

    image_logs = []

    for _, prompt in enumerate(validation_prompts):
        images = []
        with torch.autocast("cuda", dtype=weight_dtype):
            images = pipeline(
                prompt=prompt,
                num_inference_steps=inference_steps,
                num_images_per_prompt=4,
                generator=generator,
                guidance_scale=cfg,
            ).images
        image_logs.append({"validation_prompt": prompt, "images": images})

    for tracker in accelerator.trackers:
        if tracker.name == "tensorboard":
            for log in image_logs:
                images = log["images"]
                validation_prompt = log["validation_prompt"]
                formatted_images = []
                for image in images:
                    formatted_images.append(np.asarray(image))

                formatted_images = np.stack(formatted_images)

                tracker.writer.add_images(
                    validation_prompt, formatted_images, step, dataformats="NHWC"
                )
        elif tracker.name == "wandb":
            formatted_images = []

            for log in image_logs:
                images = log["images"]
                validation_prompt = log["validation_prompt"]
                for image in images:
                    image = wandb.Image(image, caption=validation_prompt)
                    formatted_images.append(image)

            tracker.log({f"validation-{cfg}-{inference_steps}": formatted_images})
        else:
            logger.warn(f"image logging not implemented for {tracker.name}")

        del pipeline
        gc.collect()
        torch.cuda.empty_cache()

        return image_logs


def append_dims(x, target_dims):
    """Appends dimensions to the end of a tensor until it has target_dims dimensions."""
    dims_to_append = target_dims - x.ndim
    if dims_to_append < 0:
        raise ValueError(
            f"input has {x.ndim} dims but target_dims is {target_dims}, which is less"
        )
    return x[(...,) + (None,) * dims_to_append]


def scalings_for_boundary_conditions_target(index, selected_indices):
    c_skip = torch.isin(index, selected_indices).float()
    c_out = 1.0 - c_skip
    return c_skip, c_out


def scalings_for_boundary_conditions_online(index, selected_indices):
    c_skip = torch.zeros_like(index).float()
    c_out = torch.ones_like(index).float()
    return c_skip, c_out


def scalings_for_boundary_conditions(timestep, sigma_data=0.5, timestep_scaling=10.0):
    c_skip = sigma_data**2 / ((timestep / 0.1) ** 2 + sigma_data**2)
    c_out = (timestep / 0.1) / ((timestep / 0.1) ** 2 + sigma_data**2) ** 0.5
    return c_skip, c_out


# Compare LCMScheduler.step, Step 4
def predicted_origin(model_output, timesteps, sample, prediction_type, alphas, sigmas):
    if prediction_type == "epsilon":
        sigmas = extract_into_tensor(sigmas, timesteps, sample.shape)
        alphas = extract_into_tensor(alphas, timesteps, sample.shape)
        pred_x_0 = (sample - sigmas * model_output) / alphas
    elif prediction_type == "v_prediction":
        sigmas = extract_into_tensor(sigmas, timesteps, sample.shape)
        alphas = extract_into_tensor(alphas, timesteps, sample.shape)
        pred_x_0 = alphas * sample - sigmas * model_output
    else:
        raise ValueError(f"Prediction type {prediction_type} currently not supported.")

    return pred_x_0


def extract_into_tensor(a, t, x_shape):
    b, *_ = t.shape
    out = a.gather(-1, t)
    return out.reshape(b, *((1,) * (len(x_shape) - 1)))


class DDIMSolver:
    def __init__(self, alpha_cumprods, timesteps=1000, ddim_timesteps=40):
        # DDIM sampling parameters
        self.step_ratio = timesteps // ddim_timesteps
        self.ddim_timesteps = (
            np.arange(1, ddim_timesteps + 1) * self.step_ratio
        ).round().astype(np.int64) - 1
        self.ddim_alpha_cumprods = alpha_cumprods[
            self.ddim_timesteps
        ]  # 40个，但是不包含0
        self.ddim_timesteps_prev = np.asarray([0] + self.ddim_timesteps[:-1].tolist())
        self.ddim_alpha_cumprods_prev = np.asarray(
            [alpha_cumprods[0]] + alpha_cumprods[self.ddim_timesteps[:-1]].tolist()
        )
        # convert to torch tensors
        self.ddim_timesteps = torch.from_numpy(self.ddim_timesteps).long()
        self.ddim_timesteps_prev = torch.from_numpy(self.ddim_timesteps_prev).long()
        self.ddim_alpha_cumprods = torch.from_numpy(self.ddim_alpha_cumprods)
        self.ddim_alpha_cumprods_prev = torch.from_numpy(self.ddim_alpha_cumprods_prev)

    def to(self, device):
        self.ddim_timesteps = self.ddim_timesteps.to(device)
        self.ddim_timesteps_prev = self.ddim_timesteps_prev.to(device)

        self.ddim_alpha_cumprods = self.ddim_alpha_cumprods.to(device)
        self.ddim_alpha_cumprods_prev = self.ddim_alpha_cumprods_prev.to(device)
        return self

    def ddim_step(
        self, pred_x0, pred_noise, timestep_index
    ):  # index需要填写的是target index - 1
        alpha_cumprod_prev = extract_into_tensor(
            self.ddim_alpha_cumprods_prev, timestep_index, pred_x0.shape
        )
        dir_xt = (1.0 - alpha_cumprod_prev).sqrt() * pred_noise
        x_prev = alpha_cumprod_prev.sqrt() * pred_x0 + dir_xt
        return x_prev

    def ddim_style_multiphase(self, pred_x0, pred_noise, timestep_index, multiphase):
        inference_indices = np.linspace(
            0, len(self.ddim_timesteps), num=multiphase, endpoint=False
        )
        inference_indices = np.floor(inference_indices).astype(np.int64)
        inference_indices = (
            torch.from_numpy(inference_indices).long().to(self.ddim_timesteps.device)
        )
        expanded_timestep_index = timestep_index.unsqueeze(1).expand(
            -1, inference_indices.size(0)
        )
        valid_indices_mask = expanded_timestep_index >= inference_indices
        last_valid_index = valid_indices_mask.flip(dims=[1]).long().argmax(dim=1)
        last_valid_index = inference_indices.size(0) - 1 - last_valid_index

        timestep_index = inference_indices[last_valid_index]
        alpha_cumprod_prev = extract_into_tensor(
            self.ddim_alpha_cumprods_prev, timestep_index, pred_x0.shape
        )
        dir_xt = (1.0 - alpha_cumprod_prev).sqrt() * pred_noise
        x_prev = alpha_cumprod_prev.sqrt() * pred_x0 + dir_xt
        return x_prev, self.ddim_timesteps_prev[timestep_index]


def import_model_class_from_model_name_or_path(
    pretrained_model_name_or_path: str, revision: str, subfolder: str = "text_encoder"
):
    text_encoder_config = PretrainedConfig.from_pretrained(
        pretrained_model_name_or_path,
        subfolder=subfolder,
        revision=revision,
        use_auth_token=True,
    )
    model_class = text_encoder_config.architectures[0]

    if model_class == "CLIPTextModel":
        from transformers import CLIPTextModel

        return CLIPTextModel
    elif model_class == "CLIPTextModelWithProjection":
        from transformers import CLIPTextModelWithProjection

        return CLIPTextModelWithProjection
    else:
        raise ValueError(f"{model_class} is not supported.")


def parse_args():
    parser = argparse.ArgumentParser(description="Simple example of a training script.")
    # ----------Model Checkpoint Loading Arguments----------
    parser.add_argument(
        "--pretrained_teacher_model",
        type=str,
        default=None,
        required=True,
        help="Path to pretrained LDM teacher model or model identifier from huggingface.co/models.",
    )
    parser.add_argument(
        "--pretrained_vae_model_name_or_path",
        type=str,
        default=None,
        help="Path to pretrained VAE model with better numerical stability. More details: https://github.com/huggingface/diffusers/pull/4038.",
    )
    parser.add_argument(
        "--teacher_revision",
        type=str,
        default=None,
        required=False,
        help="Revision of pretrained LDM teacher model identifier from huggingface.co/models.",
    )
    parser.add_argument(
        "--revision",
        type=str,
        default=None,
        required=False,
        help="Revision of pretrained LDM model identifier from huggingface.co/models.",
    )
    # ----------Training Arguments----------
    # ----General Training Arguments----
    parser.add_argument(
        "--output_dir",
        type=str,
        default="lcm-xl-distilled",
        help="The output directory where the model predictions and checkpoints will be written.",
    )
    parser.add_argument(
        "--cache_dir",
        type=str,
        default=None,
        help="The directory where the downloaded models and datasets will be stored.",
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="A seed for reproducible training."
    )
    # ----Logging----
    parser.add_argument(
        "--logging_dir",
        type=str,
        default="logs",
        help=(
            "[TensorBoard](https://www.tensorflow.org/tensorboard) log directory. Will default to"
            " *output_dir/runs/**CURRENT_DATETIME_HOSTNAME***."
        ),
    )
    parser.add_argument(
        "--report_to",
        type=str,
        default="tensorboard",
        help=(
            'The integration to report the results and logs to. Supported platforms are `"tensorboard"`'
            ' (default), `"wandb"` and `"comet_ml"`. Use `"all"` to report to all integrations.'
        ),
    )
    # ----Checkpointing----
    parser.add_argument(
        "--checkpointing_steps",
        type=int,
        default=500,
        help=(
            "Save a checkpoint of the training state every X updates. These checkpoints are only suitable for resuming"
            " training using `--resume_from_checkpoint`."
        ),
    )
    parser.add_argument(
        "--checkpoints_total_limit",
        type=int,
        default=None,
        help=("Max number of checkpoints to store."),
    )
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help=(
            "Whether training should be resumed from a previous checkpoint. Use a path saved by"
            ' `--checkpointing_steps`, or `"latest"` to automatically select the last available checkpoint.'
        ),
    )



    # ----Image Processing----
    parser.add_argument(
        "--train_shards_path_or_url",
        type=str,
        default=None,
        help=(
            "The name of the Dataset (from the HuggingFace hub) to train on (could be your own, possibly private,"
            " dataset). It can also be a path pointing to a local copy of a dataset in your filesystem,"
            " or to a folder containing files that 🤗 Datasets can understand."
        ),
    )
    parser.add_argument(
        "--resolution",
        type=int,
        default=1024,
        help=(
            "The resolution for input images, all the images in the train/validation dataset will be resized to this"
            " resolution"
        ),
    )
    parser.add_argument(
        "--use_fix_crop_and_size",
        action="store_true",
        help="Whether or not to use the fixed crop and size for the teacher model.",
        default=False,
    )
    parser.add_argument(
        "--center_crop",
        default=False,
        action="store_true",
        help=(
            "Whether to center crop the input images to the resolution. If not set, the images will be randomly"
            " cropped. The images will be resized to the resolution first before cropping."
        ),
    )
    parser.add_argument(
        "--random_flip",
        action="store_true",
        help="whether to randomly flip images horizontally",
    )
    # ----Dataloader----
    parser.add_argument(
        "--dataloader_num_workers",
        type=int,
        default=0,
        help=(
            "Number of subprocesses to use for data loading. 0 means that the data will be loaded in the main process."
        ),
    )
    # ----Batch Size and Training Steps----
    parser.add_argument(
        "--train_batch_size",
        type=int,
        default=16,
        help="Batch size (per device) for the training dataloader.",
    )
    parser.add_argument("--num_train_epochs", type=int, default=100)
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=None,
        help="Total number of training steps to perform.  If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--max_train_samples",
        type=int,
        default=None,
        help=(
            "For debugging purposes or quicker training, truncate the number of training examples to this "
            "value if set."
        ),
    )
    # ----Learning Rate----
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=1e-4,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument(
        "--scale_lr",
        action="store_true",
        default=False,
        help="Scale the learning rate by the number of GPUs, gradient accumulation steps, and batch size.",
    )
    parser.add_argument(
        "--lr_scheduler",
        type=str,
        default="constant",
        help=(
            'The scheduler type to use. Choose between ["linear", "cosine", "cosine_with_restarts", "polynomial",'
            ' "constant", "constant_with_warmup"]'
        ),
    )

    # ----Exponential Moving Average (EMA)----
    parser.add_argument(
        "--ema_decay",
        type=float,
        default=0.95,
        required=False,
        help="The exponential moving average (EMA) rate or decay factor.",
    )
    parser.add_argument(
        "--lr_warmup_steps",
        type=int,
        default=500,
        help="Number of steps for the warmup in the lr scheduler.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    # ----Optimizer (Adam)----
    parser.add_argument(
        "--use_8bit_adam",
        action="store_true",
        help="Whether or not to use 8-bit Adam from bitsandbytes.",
    )
    parser.add_argument(
        "--adam_beta1",
        type=float,
        default=0.9,
        help="The beta1 parameter for the Adam optimizer.",
    )
    parser.add_argument(
        "--adam_beta2",
        type=float,
        default=0.999,
        help="The beta2 parameter for the Adam optimizer.",
    )
    parser.add_argument(
        "--adam_weight_decay", type=float, default=1e-2, help="Weight decay to use."
    )
    parser.add_argument(
        "--adam_epsilon",
        type=float,
        default=1e-08,
        help="Epsilon value for the Adam optimizer",
    )
    parser.add_argument(
        "--max_grad_norm", default=1.0, type=float, help="Max gradient norm."
    )
    # ----Diffusion Training Arguments----
    parser.add_argument(
        "--proportion_empty_prompts",
        type=float,
        default=0,
        help="Proportion of image prompts to be replaced with empty strings. Defaults to 0 (no prompt replacement).",
    )
    # ----Latent Consistency Distillation (LCD) Specific Arguments----
    parser.add_argument(
        "--w_min",
        type=float,
        default=3.0,
        required=False,
        help=(
            "The minimum guidance scale value for guidance scale sampling. Note that we are using the Imagen CFG"
            " formulation rather than the LCM formulation, which means all guidance scales have 1 added to them as"
            " compared to the original paper."
        ),
    )
    parser.add_argument(
        "--w_max",
        type=float,
        default=15.0,
        required=False,
        help=(
            "The maximum guidance scale value for guidance scale sampling. Note that we are using the Imagen CFG"
            " formulation rather than the LCM formulation, which means all guidance scales have 1 added to them as"
            " compared to the original paper."
        ),
    )
    parser.add_argument(
        "--num_ddim_timesteps",
        type=int,
        default=50,
        help="The number of timesteps to use for DDIM sampling.",
    )
    parser.add_argument(
        "--loss_type",
        type=str,
        default="l2",
        choices=["l2", "huber"],
        help="The type of loss to use for the LCD loss.",
    )
    parser.add_argument(
        "--huber_c",
        type=float,
        default=0.001,
        help="The huber loss parameter. Only used if `--loss_type=huber`.",
    )
    parser.add_argument(
        "--lora_rank",
        type=int,
        default=64,
        help="The rank of the LoRA projection matrix.",
    )
    # ----Mixed Precision----
    parser.add_argument(
        "--mixed_precision",
        type=str,
        default=None,
        choices=["no", "fp16", "bf16"],
        help=(
            "Whether to use mixed precision. Choose between fp16 and bf16 (bfloat16). Bf16 requires PyTorch >="
            " 1.10.and an Nvidia Ampere GPU.  Default to the value of accelerate config of the current system or the"
            " flag passed with the `accelerate.launch` command. Use this argument to override the accelerate config."
        ),
    )
    parser.add_argument(
        "--allow_tf32",
        action="store_true",
        help=(
            "Whether or not to allow TF32 on Ampere GPUs. Can be used to speed up training. For more information, see"
            " https://pytorch.org/docs/stable/notes/cuda.html#tensorfloat-32-tf32-on-ampere-devices"
        ),
    )
    parser.add_argument(
        "--cast_teacher_unet",
        action="store_true",
        help="Whether to cast the teacher U-Net to the precision specified by `--mixed_precision`.",
    )
    # ----Training Optimizations----
    parser.add_argument(
        "--enable_xformers_memory_efficient_attention",
        action="store_true",
        help="Whether or not to use xformers.",
    )
    parser.add_argument(
        "--gradient_checkpointing",
        action="store_true",
        help="Whether or not to use gradient checkpointing to save memory at the expense of slower backward pass.",
    )
    # ----Distributed Training----
    parser.add_argument(
        "--local_rank",
        type=int,
        default=-1,
        help="For distributed training: local_rank",
    )
    # ----------Validation Arguments----------
    parser.add_argument(
        "--validation_steps",
        type=int,
        default=200,
        help="Run validation every X steps.",
    )
    # ----------Huggingface Hub Arguments-----------
    parser.add_argument(
        "--push_to_hub",
        action="store_true",
        help="Whether or not to push the model to the Hub.",
    )
    parser.add_argument(
        "--hub_token",
        type=str,
        default=None,
        help="The token to use to push to the Model Hub.",
    )
    parser.add_argument(
        "--hub_model_id",
        type=str,
        default=None,
        help="The name of the repository to keep in sync with the local `output_dir`.",
    )


    parser.add_argument(
        "--not_use_crop",
        action="store_true",
        help="whether or not to use crop.",
    )
    # ----------Accelerate Arguments----------
    parser.add_argument(
        "--tracker_project_name",
        type=str,
        default="text2image-fine-tune",
        help=(
            "The `project_name` argument passed to Accelerator.init_trackers for"
            " more information see https://huggingface.co/docs/accelerate/v0.17.0/en/package_reference/accelerator#accelerate.Accelerator"
        ),
    )

    parser.add_argument("--not_apply_cfg_solver", action="store_true")
    parser.add_argument("--multiphase", default=4, type=int)
    parser.add_argument("--adv_weight", default=0.1, type=float)
    parser.add_argument("--adv_lr", default=1e-5, type=float)



    #DMD2 loss相关参数
    parser.add_argument("--min_step_percent", type=float, default=0.02, help="minimum step percent for training")
    parser.add_argument("--max_step_percent", type=float, default=0.98, help="maximum step percent for training")
    parser.add_argument("--use_fp16", action="store_true")
    parser.add_argument("--real_guidance_scale", type=float, default=6.0)
    parser.add_argument("--fake_guidance_scale", type=float, default=1)
    parser.add_argument("--sdxl", action="store_true")
    parser.add_argument("--dmd_loss", action="store_true")
    parser.add_argument("--RL_epsilon", default=0.3, type=float)
    parser.add_argument("--dmd_weight", default=0.3, type=float)



    args = parser.parse_args()
    env_local_rank = int(os.environ.get("LOCAL_RANK", -1))
    if env_local_rank != -1 and env_local_rank != args.local_rank:
        args.local_rank = env_local_rank

    if args.proportion_empty_prompts < 0 or args.proportion_empty_prompts > 1:
        raise ValueError("`--proportion_empty_prompts` must be in the range [0, 1].")

    return args


# Adapted from pipelines.StableDiffusionXLPipeline.encode_prompt
def encode_prompt(
    prompt_batch, text_encoders, tokenizers, proportion_empty_prompts, is_train=True
):
    prompt_embeds_list = []

    captions = []
    for caption in prompt_batch:
        if random.random() < proportion_empty_prompts:
            captions.append("")
        elif isinstance(caption, str):
            captions.append(caption)
        elif isinstance(caption, (list, np.ndarray)):
            # take a random caption if there are multiple
            captions.append(random.choice(caption) if is_train else caption[0])

    with torch.no_grad():
        for tokenizer, text_encoder in zip(tokenizers, text_encoders):
            try:
                text_inputs = tokenizer(
                    captions,
                    padding="max_length",
                    max_length=tokenizer.model_max_length,
                    truncation=True,
                    return_tensors="pt",
                )
            except:
                import pdb; pdb.set_trace()
            text_input_ids = text_inputs.input_ids
            prompt_embeds = text_encoder(
                text_input_ids.to(text_encoder.device),
                output_hidden_states=True,
            )

            # We are only ALWAYS interested in the pooled output of the final text encoder
            pooled_prompt_embeds = prompt_embeds[0]
            prompt_embeds = prompt_embeds.hidden_states[-2]
            bs_embed, seq_len, _ = prompt_embeds.shape
            prompt_embeds = prompt_embeds.view(bs_embed, seq_len, -1)
            prompt_embeds_list.append(prompt_embeds)

    prompt_embeds = torch.concat(prompt_embeds_list, dim=-1)
    pooled_prompt_embeds = pooled_prompt_embeds.view(bs_embed, -1)
    return prompt_embeds, pooled_prompt_embeds


def main(args):
    existing_data = []
    # phased_weight_list = [1/4, 1/4, 1/4, 1/4]
    RL_start_epoch = 10
    RL_alpha = 0.1
    RL_gamma = 0.9
    n_state = 24

    agent = QLearning(n_state, args.RL_epsilon, RL_alpha, RL_gamma)




    logging_dir = Path(args.output_dir, args.logging_dir)

    accelerator_project_config = ProjectConfiguration(
        project_dir=args.output_dir, logging_dir=logging_dir
    )

    accelerator = Accelerator(
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        mixed_precision=args.mixed_precision,
        log_with=args.report_to,
        project_config=accelerator_project_config,
    )

    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    logger.info(accelerator.state, main_process_only=False)
    if accelerator.is_local_main_process:
        transformers.utils.logging.set_verbosity_warning()
        diffusers.utils.logging.set_verbosity_info()
    else:
        transformers.utils.logging.set_verbosity_error()
        diffusers.utils.logging.set_verbosity_error()

    # If passed along, set the training seed now.
    if args.seed is not None:
        set_seed(args.seed + accelerator.process_index)

    # Handle the repository creation
    if accelerator.is_main_process:
        if args.output_dir is not None:
            os.makedirs(args.output_dir, exist_ok=True)

            
        file1 = open(os.path.join(args.output_dir, f"action_history_epsilon_{args.RL_epsilon}.txt"), "a")
        file2 = open(os.path.join(args.output_dir, f"state_history_epsilon_{args.RL_epsilon}.txt"), "a")


  

        if args.push_to_hub:
            create_repo(
                repo_id=args.hub_model_id or Path(args.output_dir).name,
                exist_ok=True,
                token=args.hub_token,
                private=True,
            ).repo_id



    # 1. Create the noise scheduler and the desired noise schedule.
    noise_scheduler = DDPMScheduler.from_pretrained(
        args.pretrained_teacher_model,
        subfolder="scheduler",
        revision=args.teacher_revision,
    )

    # The scheduler calculates the alpha and sigma schedule for us
    alpha_schedule = torch.sqrt(noise_scheduler.alphas_cumprod)
    sigma_schedule = torch.sqrt(1 - noise_scheduler.alphas_cumprod)
    solver = DDIMSolver(
        noise_scheduler.alphas_cumprod.numpy(),
        timesteps=noise_scheduler.config.num_train_timesteps,
        ddim_timesteps=args.num_ddim_timesteps,
    )
    # 2. Load tokenizers from SD-XL checkpoint.
    tokenizer_one = AutoTokenizer.from_pretrained(
        args.pretrained_teacher_model,
        subfolder="tokenizer",
        revision=args.teacher_revision,
        use_fast=False,
    )
    tokenizer_two = AutoTokenizer.from_pretrained(
        args.pretrained_teacher_model,
        subfolder="tokenizer_2",
        revision=args.teacher_revision,
        use_fast=False,
    )

    # 3. Load text encoders from SD-XL checkpoint.
    # import correct text encoder classes
    text_encoder_cls_one = import_model_class_from_model_name_or_path(
        args.pretrained_teacher_model, args.teacher_revision
    )
    text_encoder_cls_two = import_model_class_from_model_name_or_path(
        args.pretrained_teacher_model, args.teacher_revision, subfolder="text_encoder_2"
    )

    text_encoder_one = text_encoder_cls_one.from_pretrained(
        args.pretrained_teacher_model,
        subfolder="text_encoder",
        revision=args.teacher_revision,
    )
    text_encoder_two = text_encoder_cls_two.from_pretrained(
        args.pretrained_teacher_model,
        subfolder="text_encoder_2",
        revision=args.teacher_revision,
    )

    print("##text_encoder loaded")

    # 4. Load VAE from SD-XL checkpoint (or more stable VAE)
    vae_path = (
        args.pretrained_teacher_model
        if args.pretrained_vae_model_name_or_path is None 
        else args.pretrained_vae_model_name_or_path
    )

    vae = AutoencoderKL.from_pretrained(
        vae_path,
        subfolder="vae" if args.pretrained_vae_model_name_or_path is None  else None,
        revision=args.teacher_revision,
    )

    # 5. Load teacher U-Net from SD-XL checkpoint
    teacher_unet = UNet2DConditionModel.from_pretrained(
        args.pretrained_teacher_model, subfolder="unet", revision=args.teacher_revision
    )

    print("##teacher_unet loaded")
    discriminator = Discriminator(teacher_unet)

    discriminator.unet.requires_grad_(False)
    teacher_unet.requires_grad_(False)
    discriminator_params = []
    for param in discriminator.heads.parameters():
        param.requires_grad = True
        discriminator_params.append(param)

    # 6. Freeze teacher vae, text_encoders, and teacher_unet
    vae.requires_grad_(False)
    text_encoder_one.requires_grad_(False)
    text_encoder_two.requires_grad_(False)

    # 8. Create online (`unet`) student U-Nets. This will be updated by the optimizer (e.g. via backpropagation.)

    unet = UNet2DConditionModel(**teacher_unet.config)
    # load teacher_unet weights into unet
    unet.load_state_dict(teacher_unet.state_dict(), strict=False)
    unet.train()

    # 9. Create target (`ema_unet`) student U-Net parameters. This will be updated via EMA updates (polyak averaging).
    # Initialize from unet
    target_unet = UNet2DConditionModel(**teacher_unet.config)
    target_unet.load_state_dict(unet.state_dict())
    target_unet.train()
    target_unet.requires_grad_(False)




    # Check that all trainable models are in full precision
    low_precision_error_string = (
        " Please make sure to always have all model weights in full float32 precision when starting training - even if"
        " doing mixed precision training, copy of the weights should still be float32."
    )

    if accelerator.unwrap_model(unet).dtype != torch.float32:
        raise ValueError(
            f"Controlnet loaded as datatype {accelerator.unwrap_model(unet).dtype}. {low_precision_error_string}"
        )

    # 8. Add LoRA to the student U-Net, only the LoRA projection matrix will be updated by the optimizer.
    lora_config = LoraConfig(
        r=args.lora_rank,
        target_modules=[
            "to_q",
            "to_k",
            "to_v",
            "to_out.0",
            "proj_in",
            "proj_out",
            "ff.net.0.proj",
            "ff.net.2",
            "conv1",
            "conv2",
            "conv_shortcut",
            "downsamplers.0.conv",
            "upsamplers.0.conv",
            "time_emb_proj",
        ],
    )

    # 9. Handle mixed precision and device placement
    # For mixed precision training we cast all non-trainable weigths to half-precision
    # as these weights are only used for inference, keeping weights in full precision is not required.
    weight_dtype = torch.float32
    if accelerator.mixed_precision == "fp16":
        weight_dtype = torch.float16
    elif accelerator.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16

    # Move unet, vae and text_encoder to device and cast to weight_dtype
    # The VAE is in float32 to avoid NaN losses.
    vae.to(accelerator.device)
    if args.pretrained_vae_model_name_or_path is not None:
        vae.to(dtype=weight_dtype)
    text_encoder_one.to(accelerator.device, dtype=weight_dtype)
    text_encoder_two.to(accelerator.device, dtype=weight_dtype)
    target_unet.to(accelerator.device)

    # Also move the alpha and sigma noise schedules to accelerator.device.
    alpha_schedule = alpha_schedule.to(accelerator.device)
    sigma_schedule = sigma_schedule.to(accelerator.device)
    solver = solver.to(accelerator.device)

    # 10. Handle saving and loading of checkpoints
    # `accelerate` 0.16.0 will have better support for customized saving
    if version.parse(accelerate.__version__) >= version.parse("0.16.0"):
        # create custom saving & loading hooks so that `accelerator.save_state(...)` serializes in a nice format
        def save_model_hook(models, weights, output_dir):
            if accelerator.is_main_process:
                target_unet.save_pretrained(os.path.join(output_dir, "unet_target"))

                for i, model in enumerate(models):

                    # #不保存判别器的权重
                    if not isinstance(model, Discriminator):
                        model.save_pretrained(os.path.join(output_dir, "unet"))
                    else:
                        torch.save(accelerator.unwrap_model(discriminator).heads.state_dict(), os.path.join(output_dir,"heads.pth"))


                    # make sure to pop weight so that corresponding model is not saved again
                    weights.pop()


        def load_model_hook(models, input_dir):
            load_model = UNet2DConditionModel.from_pretrained(os.path.join(input_dir, "unet_target"))
            target_unet.load_state_dict(load_model.state_dict())
            target_unet.to(accelerator.device)
            del load_model


            for i in range(len(models)):
                # pop models so that they are not loaded again
                model = models.pop()

                #model[1]是判别器，先被弹出
                if i == 0:
                    
                    accelerator.unwrap_model(discriminator).heads.load_state_dict(torch.load(os.path.join(input_dir,"heads.pth")))
                    continue
                # load diffusers style into model
                load_model = UNet2DConditionModel.from_pretrained(input_dir, subfolder="unet")
                model.register_to_config(**load_model.config)

                model.load_state_dict(load_model.state_dict())
                del load_model

        accelerator.register_save_state_pre_hook(save_model_hook)
        accelerator.register_load_state_pre_hook(load_model_hook)

    # 11. Enable optimizations
    if args.enable_xformers_memory_efficient_attention:
        if is_xformers_available():
            import xformers

            xformers_version = version.parse(xformers.__version__)
            if xformers_version == version.parse("0.0.16"):
                logger.warn(
                    "xFormers 0.0.16 cannot be used for training in some GPUs. If you observe problems during training, please update xFormers to at least 0.0.17. See https://huggingface.co/docs/diffusers/main/en/optimization/xformers for more details."
                )
            unet.enable_xformers_memory_efficient_attention()
            teacher_unet.enable_xformers_memory_efficient_attention()
            target_unet.enable_xformers_memory_efficient_attention()
        else:
            raise ValueError(
                "xformers is not available. Make sure it is installed correctly"
            )

    # Enable TF32 for faster training on Ampere GPUs,
    # cf https://pytorch.org/docs/stable/notes/cuda.html#tensorfloat-32-tf32-on-ampere-devices
    if args.allow_tf32:
        torch.backends.cuda.matmul.allow_tf32 = True

    if args.gradient_checkpointing:
        unet.enable_gradient_checkpointing()
        teacher_unet.enable_gradient_checkpointing()

    # Use 8-bit Adam for lower memory usage or to fine-tune the model in 16GB GPUs
    if args.use_8bit_adam:
        try:
            import bitsandbytes as bnb
        except ImportError:
            raise ImportError(
                "To use 8-bit Adam, please install the bitsandbytes library: `pip install bitsandbytes`."
            )

        optimizer_class = bnb.optim.AdamW8bit
    else:
        optimizer_class = torch.optim.AdamW

    # 12. Optimizer creation
    optimizer = optimizer_class(
        unet.parameters(),
        lr=args.learning_rate,
        betas=(args.adam_beta1, args.adam_beta2),
        weight_decay=args.adam_weight_decay,
        eps=args.adam_epsilon,
    )
    discriminator_optimizer = optimizer_class(
        discriminator_params,
        lr=args.adv_lr,
        betas=(0.0, 0.999),
        weight_decay=1e-3,
        eps=args.adam_epsilon,
    )

    # 13. Dataset creation and data processing
    # Here, we compute not just the text embeddings but also the additional embeddings
    # needed for the SD XL UNet to operate.
    @torch.no_grad()
    def compute_embeddings(
        prompt_batch,
        original_sizes,
        crop_coords,
        target_size,
        proportion_empty_prompts,
        text_encoders,
        tokenizers,
        is_train=True,
    ):
        # target_size = (args.resolution, args.resolution)
        target_size = target_size[0]
        original_sizes = list(map(list, zip(*original_sizes)))
        crops_coords_top_left = list(map(list, zip(*crop_coords)))

        original_sizes = torch.tensor(original_sizes, dtype=torch.long)
        crops_coords_top_left = torch.tensor(crops_coords_top_left, dtype=torch.long)

        prompt_embeds, pooled_prompt_embeds = encode_prompt(
            prompt_batch, text_encoders, tokenizers, proportion_empty_prompts, is_train
        )
        add_text_embeds = pooled_prompt_embeds

        # Adapted from pipeline.StableDiffusionXLPipeline._get_add_time_ids
        add_time_ids = list(target_size)
        add_time_ids = torch.tensor([add_time_ids])
        add_time_ids = add_time_ids.repeat(len(prompt_batch), 1)
        add_time_ids = torch.cat(
            [original_sizes, crops_coords_top_left, add_time_ids], dim=-1
        )
        add_time_ids = add_time_ids.to(accelerator.device, dtype=prompt_embeds.dtype)

        prompt_embeds = prompt_embeds.to(accelerator.device)
        add_text_embeds = add_text_embeds.to(accelerator.device)
        unet_added_cond_kwargs = {
            "text_embeds": add_text_embeds,
            "time_ids": add_time_ids,
        }

        return {"prompt_embeds": prompt_embeds, **unet_added_cond_kwargs}

    print("##load dataset")
    data_module = ComicDataModule(batch_size=args.train_batch_size, file_txt=args.train_shards_path_or_url)
    data_module.setup(stage="fit")
    train_dataloader = data_module.train_dataloader()
    # if args.not_use_crop:
    #     train_dataset = CustomImageDataset_without_crop(args.train_shards_path_or_url, args.resolution)
    #     print('not_use_crop\n'*5)
    # else:
    #     train_dataset = CustomImageDataset(args.train_shards_path_or_url, args.resolution)
    # print("##dataset loaded")

    # train_dataloader = DataLoader(
    #     train_dataset,
    #     shuffle=True,
    #     batch_size=args.train_batch_size,
    #     num_workers=args.dataloader_num_workers,
    # )

    # Let's first compute all the embeddings so that we can free up the text encoders
    # from memory.
    text_encoders = [text_encoder_one, text_encoder_two]
    tokenizers = [tokenizer_one, tokenizer_two]

    compute_embeddings_fn = functools.partial(
        compute_embeddings,
        proportion_empty_prompts=0.0,
        text_encoders=text_encoders,
        tokenizers=tokenizers,
    )

    # 14. LR Scheduler creation
    # Scheduler and math around the number of training steps.
    overrode_max_train_steps = False
    num_update_steps_per_epoch = math.ceil(
        len(train_dataloader) / args.gradient_accumulation_steps
    )
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        overrode_max_train_steps = True

    if args.scale_lr:
        args.learning_rate = (
            args.learning_rate
            * args.gradient_accumulation_steps
            * args.train_batch_size
            * accelerator.num_processes
        )

    lr_scheduler = get_scheduler(
        args.lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=args.lr_warmup_steps,
        num_training_steps=args.max_train_steps,
    )


    # total_sum = sum(p.data.sum() for p in unet.parameters())
    # print(f"Total sum of all parameters in UNet after load: {total_sum}")
    # total_sum = sum(p.data.sum() for p in discriminator.heads.parameters())
    # print(f"Total sum of all parameters in discriminator.heads after load: {total_sum}")


    # 15. Prepare for training
    # Prepare everything with our `accelerator`.
    print("##prepare")
    (
        unet,
        discriminator,
        optimizer,
        discriminator_optimizer,
        lr_scheduler,
        train_dataloader,
    ) = accelerator.prepare(
        unet,
        discriminator,
        optimizer,
        discriminator_optimizer,
        lr_scheduler,
        train_dataloader,
    )
    print("##prepared")

    sdguidance = SDGuidance(args,real_unet=teacher_unet,fake_unet=unet,num_train_timesteps=noise_scheduler.config.num_train_timesteps)


    # We need to recalculate our total training steps as the size of the training dataloader may have changed.
    num_update_steps_per_epoch = math.ceil(
        len(train_dataloader) / args.gradient_accumulation_steps
    )
    if overrode_max_train_steps:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    # Afterwards we recalculate our number of training epochs
    args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if accelerator.is_main_process:
        tracker_config = dict(vars(args))
        accelerator.init_trackers(args.tracker_project_name, config=tracker_config)

    # Create uncond embeds for classifier free guidance
    uncond_prompt_embeds = torch.zeros(args.train_batch_size, 77, 2048).to(
        accelerator.device
    )
    uncond_pooled_prompt_embeds = torch.zeros(args.train_batch_size, 1280).to(
        accelerator.device
    )

    # 16. Train!
    total_batch_size = (
        args.train_batch_size
        * accelerator.num_processes
        * args.gradient_accumulation_steps
    )

    logger.info("***** Running training *****")
    logger.info(f"  Num batches each epoch = {len(train_dataloader)}")
    logger.info(f"  Num Epochs = {args.num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = {args.train_batch_size}")
    logger.info(
        f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}"
    )
    logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {args.max_train_steps}")
    global_step = 0
    first_epoch = 0

    # Potentially load in the weights and states from a previous save
    if args.resume_from_checkpoint:
        if args.resume_from_checkpoint != "latest":
            path = os.path.basename(args.resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = os.listdir(args.output_dir)
            dirs = [d for d in dirs if d.startswith("checkpoint")]
            dirs = sorted(dirs, key=lambda x: int(x.split("-")[1]))
            path = dirs[-1] if len(dirs) > 0 else None

        if path is None:
            accelerator.print(
                f"Checkpoint '{args.resume_from_checkpoint}' does not exist. Starting a new training run."
            )
            args.resume_from_checkpoint = None
            initial_global_step = 0
        else:
            accelerator.print(f"Resuming from checkpoint {path}")
            accelerator.load_state(os.path.join(args.output_dir, path))
            global_step = int(path.split("-")[1])

            initial_global_step = global_step
            first_epoch = global_step // num_update_steps_per_epoch
            RL_start_epoch =  initial_global_step + 10
    else:
        initial_global_step = 0

    progress_bar = tqdm(
        range(0, args.max_train_steps),
        initial=initial_global_step,
        desc="Steps",
        # Only show the progress bar once on each machine.
        disable=not accelerator.is_local_main_process,
    )

    for epoch in range(first_epoch, args.num_train_epochs):
        for step, batch in enumerate(train_dataloader):
            if step >= len(train_dataloader) - 1:
                break
            # print(f'global_step={global_step}')

            # total_sum = sum(p.data.sum() for p in unet.parameters())
            # print(f"Total sum of all parameters in UNet after load: {total_sum}")
            # total_sum = sum(p.data.sum() for p in discriminator.heads.parameters())
            # print(f"Total sum of all parameters in discriminator.heads after load: {total_sum}")


            with accelerator.accumulate(unet, discriminator):
                # image, text, orig_size, crop_coords = batch
                # image = image.to(accelerator.device, non_blocking=True)
                # encoded_text = compute_embeddings_fn(text, orig_size, crop_coords)

                image, text, orig_size, crop_coords, target_sizes  = batch['pixel_values'], batch['caption'], batch['original_sizes'], batch['crop_top_lefts'], batch['target_sizes']     
                image = image.to(accelerator.device, non_blocking=True)
                orig_size = [torch.tensor(list(items)) for items in zip(*orig_size)]
                crop_coords = [torch.tensor(list(items)) for items in zip(*crop_coords)]
                encoded_text = compute_embeddings_fn(text, orig_size, crop_coords,target_sizes)


                if args.pretrained_vae_model_name_or_path is not None:
                    pixel_values = image.to(dtype=weight_dtype)
                    if vae.dtype != weight_dtype:
                        vae.to(dtype=weight_dtype)
                else:
                    pixel_values = image

                # encode pixel values with batch size of at most 8
                with torch.no_grad():
                    latents = []
                    for i in range(0, pixel_values.shape[0], 8):
                        latents.append(
                            vae.encode(pixel_values[i : i + 8]).latent_dist.sample()
                        )
                    latents = torch.cat(latents, dim=0)

                latents = latents * vae.config.scaling_factor
                if args.pretrained_vae_model_name_or_path is None:
                    latents = latents.to(weight_dtype)

                # Sample noise that we'll add to the latents
                noise = torch.randn_like(latents)
                bsz = latents.shape[0]

                # Sample a random timestep for each image t_n ~ U[0, N - k - 1] without bias.
                topk = (
                    noise_scheduler.config.num_train_timesteps
                    // args.num_ddim_timesteps
                )



                index = torch.randint(
                    0, args.num_ddim_timesteps, (bsz,), device=latents.device
                ).long()


                if global_step >= RL_start_epoch:
                    action, mark = agent.take_action(state)
                    if accelerator.is_main_process:
                        file1.write(f"{action}{mark}\n") 
                        file1.flush()  
                        file2.write(f"{state}\n") 
                        file2.flush()  
                    # index = action * torch.ones_like(index).cuda().long()
                    phased_weight_list = [0] * 4
                    phased_weight_list[action] = 1.0
                    index = generate_custom_random_numbers(bsz, phased_weight_list)





                start_timesteps = solver.ddim_timesteps[index]
                timesteps = start_timesteps - topk
                timesteps = torch.where(
                    timesteps < 0, torch.zeros_like(timesteps), timesteps
                )

                inference_indices = np.linspace(
                    0, len(solver.ddim_timesteps), num=args.multiphase, endpoint=False
                )
                inference_indices = np.floor(inference_indices).astype(np.int64)
                inference_indices = (
                    torch.from_numpy(inference_indices).long().to(timesteps.device)
                )

                # 20.4.4. Get boundary scalings for start_timesteps and (end) timesteps.
                c_skip_start, c_out_start = scalings_for_boundary_conditions_online(
                    index, inference_indices
                )
                c_skip_start, c_out_start = [
                    append_dims(x, latents.ndim) for x in [c_skip_start, c_out_start]
                ]
                c_skip, c_out = scalings_for_boundary_conditions_target(
                    index, inference_indices
                )
                c_skip, c_out = [append_dims(x, latents.ndim) for x in [c_skip, c_out]]

                # 20.4.5. Add noise to the latents according to the noise magnitude at each timestep
                # (this is the forward diffusion process) [z_{t_{n + k}} in Algorithm 1]
                noisy_model_input = noise_scheduler.add_noise(
                    latents, noise, start_timesteps
                )

                # 20.4.6. Sample a random guidance scale w from U[w_min, w_max] and embed it
                w = (args.w_max - args.w_min) * torch.rand((bsz,)) + args.w_min
                w = w.reshape(bsz, 1, 1, 1)
                w = w.to(device=latents.device, dtype=latents.dtype)

                # 20.4.8. Prepare prompt embeds and unet_added_conditions
                prompt_embeds = encoded_text.pop("prompt_embeds")

                # 20.4.9. Get online LCM prediction on z_{t_{n + k}}, w, c, t_{n + k}
                try:
                    noise_pred = unet(
                        noisy_model_input,
                        start_timesteps,
                        timestep_cond=None,
                        encoder_hidden_states=prompt_embeds.float(),
                        added_cond_kwargs=encoded_text,
                    ).sample
                except:
                    import pdb; pdb.set_trace()
                pred_x_0 = predicted_origin(
                    noise_pred,
                    start_timesteps,
                    noisy_model_input,
                    noise_scheduler.config.prediction_type,
                    alpha_schedule,
                    sigma_schedule,
                )

                # model_pred = c_skip_start * noisy_model_input + c_out_start * pred_x_0

                model_pred, end_timesteps = solver.ddim_style_multiphase(
                    pred_x_0, noise_pred, index, args.multiphase
                )
                # accelerator.print(index, "index")
                # accelerator.print(end_timesteps, "end_timesteps")
                model_pred = c_skip_start * noisy_model_input + c_out_start * model_pred
                # 20.4.10. Use the ODE solver to predict the kth step in the augmented PF-ODE trajectory after
                # noisy_latents with both the conditioning embedding c and unconditional embedding 0
                # Get teacher model prediction on noisy_latents and conditional embedding
                with torch.no_grad():
                    with torch.autocast("cuda"):
                        cond_teacher_output = teacher_unet(
                            noisy_model_input.float(),
                            start_timesteps,
                            encoder_hidden_states=prompt_embeds.float(),
                            added_cond_kwargs={
                                k: v.float() for k, v in encoded_text.items()
                            },
                        ).sample
                        cond_pred_x0 = predicted_origin(
                            cond_teacher_output,
                            start_timesteps,
                            noisy_model_input,
                            noise_scheduler.config.prediction_type,
                            alpha_schedule,
                            sigma_schedule,
                        )

                        if args.not_apply_cfg_solver:
                            uncond_teacher_output = cond_teacher_output
                            uncond_pred_x0 = cond_pred_x0
                        else:
                            # Get teacher model prediction on noisy_latents and unconditional embedding
                            uncond_added_conditions = copy.deepcopy(encoded_text)
                            uncond_added_conditions["text_embeds"] = (
                                uncond_pooled_prompt_embeds
                            )
                            uncond_teacher_output = teacher_unet(
                                noisy_model_input.float(),
                                start_timesteps,
                                encoder_hidden_states=uncond_prompt_embeds.float(),
                                added_cond_kwargs={
                                    k: v.float()
                                    for k, v in uncond_added_conditions.items()
                                },
                            ).sample

                            uncond_pred_x0 = predicted_origin(
                                uncond_teacher_output,
                                start_timesteps,
                                noisy_model_input,
                                noise_scheduler.config.prediction_type,
                                alpha_schedule,
                                sigma_schedule,
                            )

                        # 20.4.11. Perform "CFG" to get x_prev estimate (using the LCM paper's CFG formulation)
                        pred_x0 = cond_pred_x0 + w * (cond_pred_x0 - uncond_pred_x0)
                        pred_noise = cond_teacher_output + w * (
                            cond_teacher_output - uncond_teacher_output
                        )
                        x_prev = solver.ddim_step(pred_x0, pred_noise, index)

                # 20.4.12. Get target LCM prediction on x_prev, w, c, t_n
                with torch.no_grad():
                    with torch.autocast("cuda", dtype=weight_dtype):
                        target_noise_pred = target_unet(
                            x_prev.float(),
                            timesteps,
                            timestep_cond=None,
                            encoder_hidden_states=prompt_embeds.float(),
                            added_cond_kwargs=encoded_text,
                        ).sample
                    pred_x_0 = predicted_origin(
                        target_noise_pred,
                        timesteps,
                        x_prev,
                        noise_scheduler.config.prediction_type,
                        alpha_schedule,
                        sigma_schedule,
                    )
                    # target = c_skip * x_prev + c_out * pred_x_0

                    target, end_timesteps = solver.ddim_style_multiphase(
                        pred_x_0, target_noise_pred, index, args.multiphase
                    )
                    target = c_skip * x_prev + c_out * target
                # 20.4.13. Calculate loss

                gan_timesteps = torch.empty_like(end_timesteps)
                for i in range(end_timesteps.size(0)):
                    gan_timesteps[i] = torch.randint(
                        end_timesteps[i].item(),
                        end_timesteps[i].item()
                        + noise_scheduler.config.num_train_timesteps // args.multiphase,
                        (1,),
                        dtype=end_timesteps.dtype,
                        device=end_timesteps.device,
                    )
                real_gan = noise_scheduler.noise_travel(
                    target, torch.randn_like(latents), end_timesteps, gan_timesteps
                )
                fake_gan = noise_scheduler.noise_travel(
                    model_pred, torch.randn_like(latents), end_timesteps, gan_timesteps
                )

                if global_step % 2 == 0:
                    discriminator_optimizer.zero_grad(set_to_none=True)
                    loss = discriminator(
                        "d_loss",
                        fake_gan.float(),
                        real_gan.float(),
                        gan_timesteps,
                        prompt_embeds.float(),
                        encoded_text,
                        1,
                    )
                    accelerator.backward(loss)
                    if accelerator.sync_gradients:
                        accelerator.clip_grad_norm_(
                            discriminator.parameters(), args.max_grad_norm
                        )
                    discriminator_optimizer.step()
                    discriminator_optimizer.zero_grad(set_to_none=True)
                else:
                    if args.loss_type == "l2":
                        loss = F.mse_loss(
                            model_pred.float(), target.float(), reduction="mean"
                        )
                    elif args.loss_type == "huber":
                        diff = model_pred.float() - target.float()
                        temp_loss = torch.sqrt(diff**2 + args.huber_c**2) - args.huber_c  # 计算 Huber Loss
                        temp_loss = temp_loss.mean(dim=tuple(range(1, temp_loss.ndim)))  # 对非 batch 维度取均值
                        loss = temp_loss.mean(dim=0)
                        # loss = torch.mean(
                        #     torch.sqrt(
                        #         (model_pred.float() - target.float()) ** 2
                        #         + args.huber_c**2
                        #     )
                        #     - args.huber_c
                        # )
                    try:
                        save_data = {'global_step':global_step, 'index':index.detach().cpu().numpy().tolist(), 'temp_loss':temp_loss.detach().cpu().numpy().tolist()}
                        # 添加新数据
                        existing_data.append(save_data)
                        if global_step >= RL_start_epoch - 2:
                            sub_loss_list, phased_weight_list = process_and_plot_data(existing_data)
                            if (global_step == RL_start_epoch - 1) or (global_step == RL_start_epoch - 2):
                                state = get_rank(sub_loss_list)



                    except:
                        import pdb; pdb.set_trace()

                    g_loss = args.adv_weight * discriminator(
                        "g_loss",
                        fake_gan.float(),
                        gan_timesteps,
                        prompt_embeds.float(),
                        encoded_text,
                        1,
                    )
                    

                
                    if global_step >= RL_start_epoch:
                        next_state = get_rank(sub_loss_list)
                        agent.update(state, action, g_loss, next_state)
                        state = next_state


                    if args.dmd_loss:
                        dmd_loss = args.dmd_weight * sdguidance.compute_distribution_matching_loss(
                                    latents=latents,
                                    text_embedding=prompt_embeds,
                                    uncond_embedding=uncond_prompt_embeds,
                                    unet_added_conditions=encoded_text,
                                    uncond_unet_added_conditions=uncond_added_conditions
                                )[0]['loss_dm']
                        loss = loss + g_loss + dmd_loss
                    
                    else:
                        loss = loss + g_loss

                    # 20.4.14. Backpropagate on the online student model (`unet`)
                    accelerator.backward(loss)
                    if accelerator.sync_gradients:
                        accelerator.clip_grad_norm_(
                            unet.parameters(), args.max_grad_norm
                        )

                    optimizer.step()
                    lr_scheduler.step()
                    optimizer.zero_grad(set_to_none=True)

            # Checks if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                update_ema(target_unet.parameters(), unet.parameters(), args.ema_decay)
                progress_bar.update(1)
                global_step += 1

                if accelerator.is_main_process:
                    if global_step==1 or global_step % args.checkpointing_steps == 0:
                        # _before_ saving state, check if this save would set us over the `checkpoints_total_limit`
                        if args.checkpoints_total_limit is not None:
                            checkpoints = os.listdir(args.output_dir)
                            checkpoints = [
                                d for d in checkpoints if d.startswith("checkpoint")
                            ]
                            checkpoints = sorted(
                                checkpoints, key=lambda x: int(x.split("-")[1])
                            )

                            # before we save the new checkpoint, we need to have at _most_ `checkpoints_total_limit - 1` checkpoints
                            if len(checkpoints) >= args.checkpoints_total_limit:
                                num_to_remove = (
                                    len(checkpoints) - args.checkpoints_total_limit + 1
                                )
                                removing_checkpoints = checkpoints[0:num_to_remove]

                                logger.info(
                                    f"{len(checkpoints)} checkpoints already exist, removing {len(removing_checkpoints)} checkpoints"
                                )
                                logger.info(
                                    f"removing checkpoints: {', '.join(removing_checkpoints)}"
                                )

                                for removing_checkpoint in removing_checkpoints:
                                    removing_checkpoint = os.path.join(
                                        args.output_dir, removing_checkpoint
                                    )
                                    shutil.rmtree(removing_checkpoint)

                        save_path = os.path.join(
                            args.output_dir, f"checkpoint-{global_step}"
                        )
                        try:
                            accelerator.save_state(save_path)
                        except:
                            import pdb; pdb.set_trace()
                        logger.info(f"Saved state to {save_path}")

                    if global_step % args.validation_steps == 0:
                        log_validation(
                            vae,
                            unet,
                            args,
                            accelerator,
                            weight_dtype,
                            global_step,
                            args.multiphase,
                            1.0,
                        )
                if (global_step - 1) % 2 == 0:
                    logs = {
                        "d_loss": loss.detach().item(),
                        "lr": lr_scheduler.get_last_lr()[0],
                    }
                else:
                    if args.dmd_loss:
                        logs = {
                            "loss_cm": loss.detach().item() - g_loss.detach().item(),
                            "g_loss": g_loss.detach().item(),
                            'dmd_loss':dmd_loss.detach().item(),
                            "lr": lr_scheduler.get_last_lr()[0],
                        }
                    else:
                        logs = {
                            "loss_cm": loss.detach().item() - g_loss.detach().item(),
                            "g_loss": g_loss.detach().item(),
                            "lr": lr_scheduler.get_last_lr()[0],
                        }
                progress_bar.set_postfix(**logs)
                accelerator.log(logs, step=global_step)

                if global_step >= args.max_train_steps:
                    break

    # Create the pipeline using using the trained modules and save it.
    # accelerator.wait_for_everyone()
    # if accelerator.is_main_process:
    #     unet = accelerator.unwrap_model(unet)
    #     unet.save_pretrained(args.output_dir)
    #     lora_state_dict = get_peft_model_state_dict(unet, adapter_name="default")
    #     StableDiffusionXLPipeline.save_lora_weights(
    #         os.path.join(args.output_dir, "unet_lora"), lora_state_dict
    #     )

    # accelerator.end_training()


if __name__ == "__main__":
    args = parse_args()
    main(args)
