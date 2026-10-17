#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面 B —— ComfyUI 工具集

三个独立工具，和动漫随机生图工作台（界面 A, 端口 8190）完全分离：
    Tab1  骨架生成    骨架/参考图 -> Anima LLLite pose -> 同姿势的人
    Tab2  图变高清    Remacri / AnimeSharp 放大
    Tab3  图解析 Tag  动漫（WD14 标签） / 真人（Qwen3-VL 描述）

只连本机 ComfyUI（默认 127.0.0.1:8188），不对外暴露。

启动:
    python run.py [--port 8191] [--comfy-url http://127.0.0.1:8188] [--no-browser]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import random
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, FormData, web

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
DATA = HERE / "data"
OUTPUTS = DATA / "outputs"

# ----------------------------------------------------------------------------
# 输出文件名规范（和界面 A 保持一致的风格：文件夹按工具名，文件名=日期+序号）
# ----------------------------------------------------------------------------
# 产物按【日期建子目录】：studioB/real/<日期>/<日期>_<序号>.png
# 之前日期只在文件名里，所有图堆在一个目录，翻起来很难找。
_P = "studioB/%s/%%year%%%%month%%%%day%%/%%year%%%%month%%%%day%%"
P_POSE = _P % "pose"
P_UPSCALE = _P % "upscale"
P_TAG = _P % "tags"
# 测试产物单独一棵目录树 —— 我（助手）做验证时生成的图绝不能混进用户产出里。
P_TEST = _P % "_test"

# ComfyUI 的输出根目录。四个工具的产物都落在它下面的 studioB/<工具>/。
# 产物是 ComfyUI 写的，所以这个路径 ComfyUI 自己知道、界面B 原本不知道 ——
# 用户反馈「下载的文件不知道去哪找」，所以显式配置出来并显示在界面上。
# ComfyUI 输出目录：优先 --comfy-output 参数，其次环境变量 COMFY_OUTPUT，
# 然后依次探测常见路径。可用 --comfy-output 覆盖（见 main()）。
def _detect_comfy_output() -> Path:
    env = os.environ.get("COMFY_OUTPUT")
    if env:
        return Path(env)
    for candidate in (
        Path(r"D:\ComfyUI-ROCm\ComfyUI\output"),
        Path(r"C:\ComfyUI\output"),
        Path(r"D:\ComfyUI\output"),
        Path.home() / "ComfyUI" / "output",
    ):
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return Path(r"output")


COMFY_OUTPUT = _detect_comfy_output()
OUT_SUBDIRS = {
    "real": ("真人随机生图", "studioB/real"),
    "pose": ("骨架生成", "studioB/pose"),
    "upscale": ("图变高清", "studioB/upscale"),
    "tags": ("图解析 Tag（txt）", "studioB/tags"),
    "test": ("测试产物（助手验证用，可随时删）", "studioB/_test"),
}

# LoRA 说明表：推荐强度 + 触发词 + 所属模型族。
# ★ krea2_turbo_lora 是给【已经蒸馏过的 Turbo 模型】做两段式增强用的，官方配方是 @0.2。
#   实测把它叠到 1.0 会严重过熟：彩虹色边 + 青绿洗色 + 肌理溶解 + 五官崩解。
# ★★ 这个下拉框里混了 4 个模型族的 LoRA。LoRA 与底模不同族时，权重形状对不上，
#    轻则无效（白加），重则污染画面。所以每一条都标了 family，选错会在 used 里警告。
LORA_PRESETS = {
    "krea2_turbo_lora_rank_64_bf16.safetensors": {
        "family": "krea2", "strength": 0.2, "max_ok": 0.5,
        "note": "Turbo 专用两段式增强 LoRA，官方配方 @0.2。叠到 1.0 会严重过熟（烧图）。"
                "作用是让细节更实、脸更立体 —— 实测 @0.2 优于不用。",
    },
    "hina_krea2Turbo_lora_tqd_v4.0.safetensors": {
        "family": "krea2", "strength": 0.8,
        "note": "亚洲女性面部/表情向。无 __metadata__（来源不明）；rank 32、264 个目标模块"
                "（32 层 attn+mlp 全深度）、Kohya 扁平命名；**文本编码器权重 0 个 → 无触发词**。"
                "文件名标 krea2Turbo。",
    },
    "ultra_real_krea2_v2.safetensors": {
        "family": "krea2", "strength": 0.8,
        "note": "写实增强（UltraReal v2）。元数据：在 Krea-2-RAW 上训练、rank 32/alpha 32、"
                "**文本编码器 lr=0**（所以没有触发词，LoRA→CLIP 对它无效）、"
                "只训了 32 层 block 的 attn+mlp。作者展示图显示它主要加强高光/清漆感、"
                "背景更干净、对比与饱和度更高。",
    },
    "krea2_darkbrush.safetensors": {
        "family": "krea2", "strength": 0.8, "trigger": "monochrome ink wash style",
        "note": "水墨 / 单色笔刷风格。触发词约定是「<描述> style」，强度官方建议 0.8–1.0（原写 0.6 偏低）。",
    },
    "krea2_rainywindow.safetensors": {
        "family": "krea2", "strength": 0.8, "trigger": "rainy window style",
        "note": "雨窗 / 隔着玻璃的水痕质感。强度按官方建议 0.8–1.0。",
    },
    "krea2_sunsetblur.safetensors": {
        "family": "krea2", "strength": 0.8, "trigger": "ethereal motion blur style",
        "note": "夕阳 + 动态虚化风格，会明显糊化细节（与「人物要清晰」的目标冲突）。强度按 0.8–1.0。",
    },
    "anima-turbo-lora-v0.2.safetensors": {
        "family": "anima", "strength": 0.8,
        "note": "Anima（动漫）系的加速 LoRA，配 anima 底模用。用在 krea2 上无效。",
    },
    "anima-highres-aesthetic-boost.safetensors": {
        "family": "anima", "strength": 0.7,
        "note": "Anima 系的高分辨率审美增强，配 anima 底模用。",
    },
    "anima-rl-v0.1.safetensors": {
        "family": "anima", "strength": 0.7,
        "note": "Anima 系的 RL 调优版本，配 anima 底模用。",
    },
    "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors": {
        "family": "qwen-edit", "strength": 1.0,
        "note": "Qwen-Image-Edit 的 4 步加速 LoRA，属于图像编辑模型那一套，不是 krea2 用的。",
    },
    "add-detail-xl.safetensors": {
        "family": "sdxl", "strength": 0.6,
        "note": "SDXL 生态的「加细节」LoRA。与 krea2 不同族，跨族使用大概率无效或污染画面。",
    },
}

# 底模 → 模型族（用于判断 LoRA 是否跨族）
UNET_FAMILY = {
    "krea2_turbo_int8_convrot.safetensors": "krea2",
    "krea2_raw_int8_convrot.safetensors": "krea2",
    "anima-base-v1.0.safetensors": "anima",
    "anima-turbo-v1.1.safetensors": "anima",
    "z_image_turbo_int8_convrot.safetensors": "zimage",
    "flux-2-klein-4b.safetensors": "flux",
    "flux-2-klein-base-4b.safetensors": "flux",
}

# 剥离 WD14 混在开头的 rating（不该进提示词）
RATING_STRIP = ("general, ", "sensitive, ", "questionable, ", "explicit, ")

DESCRIBE_PROMPT = (
    "Describe this image in rich detail as a text-to-image prompt. "
    "Cover: subject and appearance, clothing, pose and action, environment, "
    "lighting and time of day, camera angle and lens, color palette and mood. "
    "Keep the skin smooth and clean in your description; do not mention pores, "
    "skin texture, freckles or grain. Reply with the description only."
)


# ============================================================================
# ComfyUI 客户端
# ============================================================================
class ComfyError(RuntimeError):
    pass


class Comfy:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self._session: ClientSession | None = None

    async def session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            self._session = ClientSession(timeout=ClientTimeout(total=1800))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _json(self, method: str, path: str, **kw):
        s = await self.session()
        async with s.request(method, self.url + path, **kw) as r:
            body = await r.text()
            if r.status >= 400:
                raise ComfyError("ComfyUI %s %s -> %s: %s" % (method, path, r.status, body[:300]))
            return json.loads(body) if body.strip() else {}

    async def status(self) -> dict:
        try:
            s = await self.session()
            async with s.get(self.url + "/system_stats", timeout=ClientTimeout(total=15)) as r:
                d = await r.json()
            dev = (d.get("devices") or [{}])[0]
            return {"online": True,
                    "version": (d.get("system") or {}).get("comfyui_version"),
                    "device": "%s %s" % (dev.get("type", ""), dev.get("name", "")),
                    "vram_total_gb": round((dev.get("vram_total") or 0) / 1e9, 2),
                    "vram_free_gb": round((dev.get("vram_free") or 0) / 1e9, 2)}
        except Exception as e:
            return {"online": False, "error": "%s: %s" % (type(e).__name__, str(e)[:120])}

    async def object_info(self, node: str | None = None) -> dict:
        return await self._json("GET", "/object_info" + ("/" + node if node else ""))

    @staticmethod
    def combo(raw: dict, node: str, field: str) -> list[str]:
        """读 COMBO 选项。两种编码都要处理：
        [[选项...], {...}]  或  ["COMBO", {"options": [...]}]"""
        try:
            spec = raw[node]["input"]["required"][field]
        except (KeyError, TypeError):
            return []
        if isinstance(spec[0], list):
            return [str(x) for x in spec[0]]
        if spec[0] == "COMBO" and isinstance(spec[1], dict):
            return [str(x) for x in (spec[1].get("options") or [])]
        return []

    async def upload(self, filename: str, content: bytes, subfolder: str = "") -> str:
        """上传到 ComfyUI input 目录，返回可用的文件名"""
        form = FormData()
        form.add_field("image", content, filename=filename, content_type="application/octet-stream")
        form.add_field("overwrite", "true")
        if subfolder:
            form.add_field("subfolder", subfolder)
        s = await self.session()
        async with s.post(self.url + "/upload/image", data=form) as r:
            body = await r.text()
            if r.status >= 400:
                raise ComfyError("上传失败 %s: %s" % (r.status, body[:300]))
            d = json.loads(body)
        name = d.get("name") or filename
        sub = d.get("subfolder") or ""
        return ("%s/%s" % (sub, name)) if sub else name

    async def submit(self, graph: dict) -> str:
        d = await self._json("POST", "/prompt", json={"prompt": graph})
        pid = d.get("prompt_id")
        if not pid:
            raise ComfyError("提交失败: %s" % json.dumps(d, ensure_ascii=False)[:300])
        return pid

    async def history(self, pid: str) -> dict | None:
        d = await self._json("GET", "/history/" + pid)
        return d.get(pid)

    async def interrupt(self):
        try:
            await self._json("POST", "/interrupt", json={})
        except Exception:
            pass

    async def clear_queue(self):
        """清掉 ComfyUI 里还没开始跑的排队任务（配合 interrupt 才算真停）。"""
        try:
            await self._json("POST", "/queue", json={"clear": True})
        except Exception:
            try:
                await self._json("POST", "/interrupt", json={})
            except Exception:
                pass

    async def view(self, filename: str, subfolder: str = "", type_: str = "output") -> tuple[bytes, str]:
        s = await self.session()
        params = {"filename": filename, "subfolder": subfolder or "", "type": type_}
        async with s.get(self.url + "/view", params=params) as r:
            if r.status >= 400:
                raise ComfyError("取图失败 %s" % r.status)
            return await r.read(), r.headers.get("Content-Type", "image/png")


# ============================================================================
# 工作流构造
# ============================================================================
def wf_pose(*, image: str, prompt: str, negative: str, patch: str, strength: float,
            width: int, height: int, steps: int, cfg: float, seed: int,
            sampler: str, scheduler: str, unet: str, clip: str, vae: str,
            lora: str = "", lora_strength: float = 1.0,
            extract: bool = False, extract_mode: str = "Full") -> dict:
    """骨架生成。extract=True 时先从参考图提取骨架（黑底纯骨架），
    否则把输入图直接当作骨架使用。"""
    g: dict = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "2": {"class_type": "ModelPatchLoader", "inputs": {"name": patch}},
        "3": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": clip, "type": "stable_diffusion", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "5": {"class_type": "LoadImage", "inputs": {"image": image}},
    }
    # 可选：LoRA
    model_src: list = ["1", 0]
    if lora:
        g["20"] = {"class_type": "LoraLoaderModelOnly",
                   "inputs": {"model": ["1", 0], "lora_name": lora,
                              "strength_model": lora_strength}}
        model_src = ["20", 0]

    if extract:
        # 参考图 -> 缩放到目标尺寸 -> 提取骨架（ImgUtilsPose 已打补丁：输出黑底纯骨架）
        g["6"] = {"class_type": "ImageScale",
                  "inputs": {"image": ["5", 0], "upscale_method": "lanczos",
                             "width": width, "height": height, "crop": "center"}}
        g["7"] = {"class_type": "ImgUtilsPose",
                  "inputs": {"image": ["6", 0], "mode": extract_mode, "auto_detect": True}}
        skel = ["7", 0]
        # 把提取的骨架也存一份，便于确认（可选关掉）
        g["8"] = {"class_type": "SaveImage",
                  "inputs": {"images": ["7", 0], "filename_prefix": P_POSE + "_骨架"}}
    else:
        # 输入本身就是骨架图，只做尺寸对齐
        g["6"] = {"class_type": "ImageScale",
                  "inputs": {"image": ["5", 0], "upscale_method": "lanczos",
                             "width": width, "height": height, "crop": "disabled"}}
        skel = ["6", 0]

    g["9"] = {"class_type": "AnimaLLLiteApply",
              "inputs": {"model": model_src, "model_patch": ["2", 0], "image": skel,
                         "strength": strength, "start_percent": 0.0, "end_percent": 1.0}}
    g["10"] = {"class_type": "CLIPTextEncode",
               "inputs": {"clip": ["3", 0], "text": prompt}}
    g["11"] = {"class_type": "CLIPTextEncode",
               "inputs": {"clip": ["3", 0], "text": negative}}
    g["12"] = {"class_type": "EmptyLatentImage",
               "inputs": {"width": width, "height": height, "batch_size": 1}}
    g["13"] = {"class_type": "KSampler",
               "inputs": {"model": ["9", 0], "positive": ["10", 0], "negative": ["11", 0],
                          "latent_image": ["12", 0], "seed": seed, "steps": steps, "cfg": cfg,
                          "sampler_name": sampler, "scheduler": scheduler, "denoise": 1.0}}
    g["14"] = {"class_type": "VAEDecode",
               "inputs": {"samples": ["13", 0], "vae": ["4", 0]}}
    g["15"] = {"class_type": "SaveImage",
               "inputs": {"images": ["14", 0], "filename_prefix": P_POSE}}
    return g


def wf_upscale(*, image: str, model: str, mode: str, value: float,
               width: int = 0, height: int = 0) -> dict:
    """图变高清。mode='scale' 按倍率，mode='target' 按目标边长（长边）。"""
    g = {
        "1": {"class_type": "LoadImage", "inputs": {"image": image}},
        "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model}},
        "3": {"class_type": "ImageUpscaleWithModel",
              "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
    }
    if mode == "target" and width and height:
        g["4"] = {"class_type": "ImageScale",
                  "inputs": {"image": ["3", 0], "upscale_method": "lanczos",
                             "width": int(width), "height": int(height), "crop": "disabled"}}
        src = ["4", 0]
    else:
        src = ["3", 0]
    g["5"] = {"class_type": "SaveImage",
              "inputs": {"images": src, "filename_prefix": P_UPSCALE}}
    return g


def wf_tag_anime(*, image: str, general_threshold: float = 0.35,
                 character_threshold: float = 0.85) -> dict:
    """动漫反推：WD14 -> 剥掉 rating -> 输出标签"""
    g = {
        "1": {"class_type": "LoadImage", "inputs": {"image": image}},
        "2": {"class_type": "ImgUtilsWD14",
              "inputs": {"image": ["1", 0], "general_threshold": general_threshold,
                         "character_threshold": character_threshold, "drop_overlap": False}},
    }
    prev = ["2", 0]
    nid = 30
    for r in RATING_STRIP:
        g[str(nid)] = {"class_type": "StringReplace",
                       "inputs": {"string": prev, "find": r, "replace": ""}}
        prev = [str(nid), 0]
        nid += 1
    g["3"] = {"class_type": "PreviewAny", "inputs": {"source": prev}}
    g["4"] = {"class_type": "SaveText",
              "inputs": {"text": prev, "filename_prefix": P_TAG, "format": "txt"}}
    return g


def wf_tag_real(*, image: str, clip: str, max_length: int = 700) -> dict:
    """真人反推：Qwen3-VL 直接看图生成描述（保留光照/机位/色调）"""
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": image}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": clip, "type": "krea2", "device": "default"}},
        "3": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": DESCRIBE_PROMPT}},
        "4": {"class_type": "TextGenerate",
              "inputs": {"clip": ["2", 0], "prompt": ["3", 0], "max_length": max_length,
                         "sampling_mode": "off", "image": ["1", 0],
                         "use_default_template": True}},
        "5": {"class_type": "PreviewAny", "inputs": {"source": ["4", 0]}},
        "6": {"class_type": "SaveText",
              "inputs": {"text": ["4", 0], "filename_prefix": P_TAG, "format": "txt"}},
    }


# ============================================================================
# 真人随机生图（Krea2）
# ============================================================================
P_REAL = _P % "real"


# 宽高比预设 → (宽比, 高比)。自己定义，不依赖 ComfyUI 的 COMBO（object_info 只回 "COMBO"）。
ASPECT_RATIOS = {
    "2:3": (2, 3),      # 竖版标准（原默认 1024x1536）
    "3:4": (3, 4),      # 竖版偏方 —— 坐姿/全身取景留腿空间更多
    "9:16": (9, 16),    # 手机竖屏
    "1:1": (1, 1),      # 方形
    "4:3": (4, 3),      # 横版偏方
    "3:2": (3, 2),      # 横版标准
    "16:9": (16, 9),    # 宽屏
}


def resolve_size(aspect: str, megapixels: float = 1.5, multiple: int = 8) -> tuple:
    """按「宽高比 + 目标百万像素」算尺寸，并对齐到 multiple 的整数倍。

    对齐是硬要求：潜空间尺寸不是 8 的倍数会直接报错或产生错位。
    对应 ComfyUI 的 ResolutionSelector 节点（宽高比 + megapixels + multiple）。
    """
    r = ASPECT_RATIOS.get(str(aspect or "").strip())
    if not r:
        for k, v in ASPECT_RATIOS.items():
            if aspect and (str(aspect) in k or k in str(aspect)):
                r = v
                break
    if not r:
        r = (2, 3)
    wr, hr = r
    mp = max(0.1, min(16.0, float(megapixels or 1.5)))
    total = mp * 1_000_000.0
    h = (total * hr / wr) ** 0.5
    w = total / h
    m = max(8, int(multiple or 8))
    return max(m, int(round(w / m)) * m), max(m, int(round(h / m)) * m)


def wf_real(*, prompt: str, negative: str, width: int, height: int, steps: int,
            cfg: float, seed: int, sampler: str = "er_sde", scheduler: str = "simple",
            unet: str = "krea2_turbo_int8_convrot.safetensors",
            clip: str = "qwen3vl_4b_fp8_scaled.safetensors",
            vae: str = "qwen_image_vae.safetensors",
            batch: int = 1, lora: str = "", lora_strength: float = 1.0,
            lora_clip: float = 0.0, loras: list | None = None,
            sharpen: float = 0.0,
            refine: float = 0.0, refine_steps: int = 4, refine_scale: float = 1.0,
            upscale_model: str = "4x_foolhardy_Remacri.safetensors",
            filename_prefix: str = "") -> dict:
    """Krea2 真人随机生图。

    两条配对约束（见 模型观察记录.md §3，别只做一半）：
      cfg == 1.0  -> 负面被完全抵消，必须用 ConditioningZeroOut 置零
      cfg  > 1.0  -> 负面才生效，必须用真实负面文本
    「置零负面 + 高 cfg」等于把条件放大 cfg 倍，会出满屏噪点。

    sharpen > 0 时在 VAEDecode 与 SaveImage 之间插一个核心 ImageSharpen
    （官方配方里有这一步，本机一直漏做）。默认 0 = 关，因为它可能和
    「光滑干净的写实」偏好冲突，必须能 A/B 对比而不是默认开。

    lora_clip > 0：改用 LoraLoader（而非 LoraLoaderModelOnly），把 LoRA 也作用到
    文本编码器。默认 0 = 不作用（保持原行为），因为「这样能让风格 LoRA 的触发词
    更有效」目前只是**假设**，未实测。

    refine > 0：两段式精修，照抄参考工作流的结构 ——
      VAEDecode → 放大模型 → 缩小 0.5（净 2x）→ VAEEncode → 低去噪重绘 → VAEDecode
    注意第二段的 model 用**原始 UNET（不带 LoRA）**，避免风格被二次强化（参考工作流
    就是这么连的：第一段取 LoRA 输出，第二段直取 UNETLoader）。
    """
    g: dict = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": clip, "type": "krea2", "device": "default"}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["2", 0], "text": prompt}},
        "4": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["2", 0], "text": negative}},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "6": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": batch}},
        "7": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["3", 0], "negative": ["4", 0],
                         "latent_image": ["6", 0], "seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": sampler, "scheduler": scheduler, "denoise": 1.0}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["7", 0], "vae": ["5", 0]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"images": ["8", 0], "filename_prefix": filename_prefix or P_REAL}},
    }
    # ── LoRA：支持【多个串联】 ─────────────────────────────────────────
    # 单个 lora/lora_strength 的旧接口保留；loras 列表优先。
    # 每一项 {"name":..., "strength":..., "clip":...}；clip>0 时该项同时作用到
    # 文本编码器（LoraLoader），否则只作用 UNET（LoraLoaderModelOnly）。
    # 注意：普通串联对"身份类" LoRA 会串味（身份互相渗透）—— 要空间隔离得用
    # 社区的区域掩码节点。风格类 LoRA 串联是安全的。
    _chain = []
    if loras:
        for _it in loras:
            if isinstance(_it, dict) and _it.get("name"):
                _chain.append({"name": _it["name"],
                               "strength": float(_it.get("strength") or 1.0),
                               "clip": float(_it.get("clip") or 0.0)})
    if not _chain and lora:
        _chain.append({"name": lora, "strength": float(lora_strength or 1.0),
                       "clip": float(lora_clip or 0.0)})

    model_src = ["1", 0]
    clip_src = ["2", 0]
    for _i, _it in enumerate(_chain):
        _nid = str(100 + _i)
        if _it["clip"] > 0:
            g[_nid] = {"class_type": "LoraLoader",
                       "inputs": {"model": model_src, "clip": clip_src,
                                  "lora_name": _it["name"],
                                  "strength_model": _it["strength"],
                                  "strength_clip": _it["clip"]}}
            model_src = [_nid, 0]
            clip_src = [_nid, 1]
        else:
            g[_nid] = {"class_type": "LoraLoaderModelOnly",
                       "inputs": {"model": model_src, "lora_name": _it["name"],
                                  "strength_model": _it["strength"]}}
            model_src = [_nid, 0]
    if _chain:
        g["7"]["inputs"]["model"] = model_src
        if clip_src != ["2", 0]:
            g["3"]["inputs"]["clip"] = clip_src
            g["4"]["inputs"]["clip"] = clip_src

    last = "8"
    if refine and refine > 0:
        # ★ 默认 refine_scale=1.0：直接在原分辨率上低去噪重绘，不放大。
        #   实测教训：照抄参考工作流的「4x 放大 → 缩 0.5」，在 1064x1416 起手时
        #   精修分辨率会到 2128x2832（6MP），单张耗时 ~600 秒（对比单段 71 秒），
        #   完全不可用。参考工作流从 ~512x768 起手，净尺寸小得多才划算。
        #   refine_scale > 1 时才走放大链，明确写出来让用户自己承担时间。
        if refine_scale and refine_scale > 1.0:
            g["30"] = {"class_type": "UpscaleModelLoader",
                       "inputs": {"model_name": upscale_model}}
            g["31"] = {"class_type": "ImageUpscaleWithModel",
                       "inputs": {"upscale_model": ["30", 0], "image": ["8", 0]}}
            g["32"] = {"class_type": "ImageScaleBy",
                       "inputs": {"image": ["31", 0], "upscale_method": "bicubic",
                                  "scale_by": float(refine_scale) / 4.0}}
            _src = ["32", 0]
        else:
            _src = ["8", 0]
        g["33"] = {"class_type": "VAEEncode",
                   "inputs": {"pixels": _src, "vae": ["5", 0]}}
        g["34"] = {"class_type": "KSampler",
                   "inputs": {"model": ["1", 0],             # ★ 原始 UNET，不带 LoRA
                              "positive": ["3", 0], "negative": ["4", 0],
                              "latent_image": ["33", 0], "seed": seed + 1,
                              "steps": max(1, int(refine_steps)), "cfg": cfg,
                              "sampler_name": sampler, "scheduler": scheduler,
                              "denoise": float(refine)}}
        g["35"] = {"class_type": "VAEDecode",
                   "inputs": {"samples": ["34", 0], "vae": ["5", 0]}}
        last = "35"
        g["9"]["inputs"]["images"] = ["35", 0]

    if sharpen and sharpen > 0:
        g["15"] = {"class_type": "ImageSharpen",
                   "inputs": {"image": [last, 0], "sharpen_radius": 1,
                              "sigma": 0.5, "alpha": float(sharpen)}}
        g["9"]["inputs"]["images"] = ["15", 0]
    return g


_REAL_POOLS: dict = {}
_REAL_CHARS: dict = {}

# 所有槽统一取值语义：'random'（或缺失/空串）= 随机 / 'off' = 不写 / 其他 = 指定条目
BASE_SLOTS = ["person", "body", "pose", "scene", "expression", "light"]

# 拆分服装槽。此处顺序 = 拼装成中文时的语序（发型→发饰→帽子→上/下装→外搭→袜→鞋→包→首饰）
SPLIT_SLOT_ORDER = ["hair", "hair_acc", "hat", "top", "bottom", "dress",
                    "outerwear", "legwear", "footwear", "bag", "jewelry"]

# 身材细分槽（拼进人物句子的 {body_extra}），顺序即语序。
# ⚠️ 槽名不能与请求参数同名：身高槽原本叫 height，与「图片像素高度 height」撞车，
#    前端把槽值铺进请求体时把 height 覆盖成 'off'，后端 int('off') 直接 500。
#    所以这里叫 stature。
BODY_DETAIL_SLOTS = ["stature", "bust", "hip", "thigh"]

# ★ 内容边界（硬性）：抽到这些未成年人类别时，强制排除泳装/内衣/丝袜类服装，
#   并清空身材与身材细分描述。项目文档已写明不做露骨内容，这条规则不允许绕过。
ADULT_ITEM_KEYWORDS = ("比基尼", "泳装", "泳衣", "内衣", "蕾丝", "吊带", "湿身",
                       "丝袜", "渔网", "连裤袜", "过膝", "大腿袜", "情趣",
                       "透明", "露背", "高开叉", "深V", "低胸", "胸罩", "内裤")
MINOR_SAFE_NOTE = ("检测到未成年人类别「%s」 → 已自动排除泳装/内衣/丝袜类服装，"
                   "并关闭身材与身材细分（胸/臀/大腿）描述")


def _as_int(v, default: int) -> int:
    """宽松转 int —— 参数被别的东西覆盖时降级到默认值，而不是整个请求 500。"""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _as_float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _is_adult_item(text: str) -> bool:
    """是否属于需要为未成年类别排除的条目（泳装/内衣/丝袜等）。

    内置表与 real_pools.json 的 adult_item_keywords 取【并集】——
    踩过一次：把关键词表改成读 JSON 后，JSON 里只写了新词，
    原有的丝袜/渔网/连裤袜等被静默丢掉，未成年过滤出现漏网。
    取并集后任一来源漏写都不会降低覆盖。
    """
    kws = set(ADULT_ITEM_KEYWORDS) | set(_REAL_POOLS.get("adult_item_keywords") or [])
    t = text or ""
    return any(k in t for k in kws)

# 只有显式的这几个值才表示「不写」。空串必须按「随机」处理 ——
# 旧前端对「随机」就是传空串，改了会静默把所有槽关掉。
_OFF_VALUES = {"off", "none", "不写"}

# 拆分搭配、且用户没有明确指定上衣/下衣/连衣裙时，随机选「连衣裙」轮廓的概率。
# 不要太高：连衣裙是单件成套装，比例过大就会让「上衣+下衣」的组合几乎抽不到。
DRESS_RATIO = 0.35


def _slot_want(choice: dict, key: str) -> str:
    """槽取值归一化：缺失 / None / 空串 -> 'random'；其余原样返回。"""
    w = choice.get(key)
    if w is None or w == "":
        return "random"
    return str(w)


def _slot_sel(choice: dict, key: str):
    """把一个槽的取值归一化成三态：(mode, selected)

      ("all",  None)      从该槽全部条目里随机（默认）
      ("off",  None)      这一项不写
      ("pick", [条目...]) 只从用户勾选的子集里随机

    ★ 这是用户真正要的语义：勾几套 → 从那几套里抽。
      「勾 1 条」只是「勾 N 条」的特例，所以不需要单独的「指定」模式。
    仍然兼容旧的单字符串写法（会当成 1 条的白名单）。
    """
    v = choice.get(key)
    if v is None or v == "":
        return ("all", None)
    if isinstance(v, (list, tuple, set)):
        items = [str(x).strip() for x in v]
        items = [x for x in items if x and x not in _OFF_VALUES]
        if not items:
            return ("off", None)
        return ("pick", items)
    s = str(v).strip()
    if s in _OFF_VALUES:
        return ("off", None)
    if s in ("random", "all"):
        return ("all", None)
    return ("pick", [s])


def load_real_data():
    global _REAL_POOLS, _REAL_CHARS
    p = HERE / "real_pools.json"
    if p.is_file():
        _REAL_POOLS = json.loads(p.read_text(encoding="utf-8"))
    c = HERE / "real_characters.json"
    if c.is_file():
        _REAL_CHARS = json.loads(c.read_text(encoding="utf-8")).get("series", {})


def real_options() -> dict:
    """返回给前端的选项清单。

    注意：条目池现在返回【完整 items】（不再只给 4 条样例），
    因为界面要支持「逐槽指定具体条目」（例如指定黑丝 + 皮短裤）。
    categories 也返回完整数组，另外给 category_counts 方便前端显示条数。
    """
    pools = _REAL_POOLS.get("pools", {})
    out = {"quality": _REAL_POOLS.get("quality", {}),
           "templates": _REAL_POOLS.get("templates", {}),
           "param_presets": _REAL_POOLS.get("param_presets", []),
           "characters": _REAL_CHARS,
           "clothing_modes": _REAL_POOLS.get("clothing_modes", {}),
           "slot_rules": _REAL_POOLS.get("slot_rules", {}),
           "slot_order": SPLIT_SLOT_ORDER,
           "lora_presets": LORA_PRESETS,
           # 宽高比 + 百万像素（对应 ComfyUI 的 ResolutionSelector 思路）
           "aspect_ratios": list(ASPECT_RATIOS.keys()),
           "default_megapixels": 1.5,
           # 两段式精修可用的放大模型（写实优先 4x_foolhardy_Remacri）
           "upscale_models": ["4x_foolhardy_Remacri.safetensors",
                              "4x-AnimeSharp.safetensors",
                              "2x-AnimeSharpV4_RCAN.safetensors"],
           "default_upscale": "4x_foolhardy_Remacri.safetensors",
           "base_slots": BASE_SLOTS + list(_REAL_POOLS.get("body_detail_slots")
                                           or BODY_DETAIL_SLOTS),
           "body_detail_slots": list(_REAL_POOLS.get("body_detail_slots")
                                     or BODY_DETAIL_SLOTS),
           "minor_person_categories": list(_REAL_POOLS.get("minor_person_categories") or []),
           "pools": {}}
    for key, pool in pools.items():
        entry = {"label": pool.get("label", key), "hint": pool.get("hint", "")}
        if "categories" in pool:
            cats = pool["categories"]
            entry["categories"] = {k: list(v) for k, v in cats.items()}
            entry["category_counts"] = {k: len(v) for k, v in cats.items()}
            entry["items"] = [t for arr in cats.values() for t in arr]
            entry["total"] = len(entry["items"])
        elif "bundles" in pool:
            entry["bundles"] = [b["name"] for b in pool["bundles"]]
            entry["total"] = len(pool["bundles"])
        else:
            entry["items"] = list(pool.get("items") or [])
            entry["total"] = len(entry["items"])
        out["pools"][key] = entry
    return out


# 取景负面兜底（用于「固定提示词」模式）：
# 这条路径不经过 draw_real，取景包的 negative 不会进来 ——
# 实测后果：提示词写了「从头顶到膝盖的七分身」，但负面里没有任何反制，
# 模型直接裁成上半身，黑丝和鞋都看不见。
FRAME_NEG_RULES = [
    ("从头顶到膝盖", "只拍到上半身, 半身照, 面部特写, 镜头过近, 裁切到头顶, 裁切到膝盖"),
    ("从头到脚", "半身照, 特写, 镜头过近, 人物占满画面, 裁切到脚部, 裁切到膝盖"),
    ("到膝盖的七分身", "只拍到上半身, 半身照, 面部特写"),
    ("腰部以上", "全身, 裁切到腿部, 下半身缺失"),
    ("胸部以上", "全身, 半身照, 裁切到腰部"),
]

# 场景带水元素时会污染人物 —— 尤其叠加 krea2_rainywindow 这个「雨窗」LoRA。
# 实测：场景「淋浴间外，玻璃上有水珠」+ rainywindow@0.6 → 水珠全跑到衣服和皮肤上。
WATER_WORDS = ("水珠", "水滴", "水汽", "雨", "淋浴", "雾气", "湿", "泳池", "海边", "浴缸")
WATER_NEG = "身上的水珠, 皮肤上的水珠, 脸上的水滴, 衣服上的水珠"


def _bust_guard(prompt_text: str, bust_value: str, pool: dict) -> tuple[str, str]:
    """用户明确要「丰满」时，往负面里加平胸项。

    反馈：胸部「完全看不出来」。光在正向堆形容词（丰满/饱满）模型抓不住，
    因为「不丰满」本身没有反向约束 —— 加「平胸 / 胸部平坦」到负面才是有效杠杆。
    """
    words = pool.get("bust_full_words") or ["丰满", "饱满", "立体", "丰盈", "撑起", "弧度"]
    neg = pool.get("bust_flat_negative") or "平胸, 胸部平坦, 身材单薄"
    src = (bust_value or "") + " " + (prompt_text or "")
    if any(w in src for w in words):
        return neg, "识别到「丰满」诉求 → 负面已加：%s" % neg
    return "", ""


# 薄纱 / 透明类衣料的走光防护。
# 实测：提示词写「极贴身的黑色薄纱长袖上衣，材质薄透，紧紧裹住上半身 + 上围饱满」时，
# 模型会直接画出透过薄纱裸露的胸部 —— 越过了项目边界（泳装/内衣可以，露骨裸体不做），
# 而且同时是坏图。所以只要提示词出现薄纱类词，就自动压制「透出皮肤」。
SHEER_WORDS = ("薄纱", "透明", "薄透", "半透明", "网纱", "若隐若现", "湿身")
SHEER_NEG = "透出胸部, 露出胸部, 走光, 裸露, 露点, 未穿内衣, 没有遮挡的胸部"


def _sheer_guard(prompt_text: str) -> tuple[str, str]:
    """薄纱类衣料 → 强制压制透出皮肤。"""
    hit = next((w for w in SHEER_WORDS if w in (prompt_text or "")), "")
    if not hit:
        return "", ""
    return SHEER_NEG, ("衣料含「%s」→ 已加负面压制走光/裸露（薄纱 + 贴身容易直接画出裸体）" % hit)


def _pose_guard(pose_value: str) -> tuple[str, str]:
    """「双腿分开」类坐姿 → 压制裆部/胯部特写。

    这类姿势本身是正常的坐姿（拍照常用），但配合裙装/内衣时容易被画成
    裆部特写 —— 已经出过一次越线的图，所以固定加一道防护，把镜头拉回人物。
    """
    pf = _REAL_POOLS.get("pose_framing") or {}
    words = pf.get("legs_apart_words") or ["双腿分开", "大开", "岔开"]
    if not any(w in (pose_value or "") for w in words):
        return "", ""
    neg = pf.get("legs_apart_negative") or "裆部特写, 胯部特写, 走光"
    note = "坐姿含「双腿分开」→ 已加负面压制裆部/胯部特写，避免变成局部特写"
    return neg, note


def _apply_plan_guards(plan: dict, lora: str) -> None:
    """给一套提示词打上「负面兜底」并写进 used。

    抽词模式（/api/real/draw 预览）和实际生成（/api/real）必须走同一套，
    否则界面上看到的负面和真正用的负面不一致。
    要补的三条：
      ① 景别 → 取景负面（固定提示词模式绕过了 draw_real，取景包负面会丢）
      ② 水元素 → 压制「水珠跑到身上」
      ③ 明确要丰满 → 负面加「平胸 / 胸部平坦」
    """
    u = plan.setdefault("used", {})
    text = plan.get("prompt") or ""
    neg = plan.get("negative") or ""

    fneg, fkey = _frame_negative_for(text)
    if fneg:
        neg = (neg + ", " + fneg) if neg else fneg
        u["frame_negative_added"] = "识别到景别「%s」→ 已补取景负面" % fkey

    wneg, wnote = _water_guard(text, lora)
    if wneg:
        neg = (neg + ", " + wneg) if neg else wneg
        u["water_negative_added"] = wnote

    bneg, bnote = _bust_guard(text, str(u.get("bust") or ""), _REAL_POOLS)
    if bneg:
        neg = (neg + ", " + bneg) if neg else bneg
        u["bust_negative_added"] = bnote

    sneg, snote = _sheer_guard(text)
    if sneg:
        neg = (neg + ", " + sneg) if neg else sneg
        u["sheer_negative_added"] = snote

    pneg, pnote = _pose_guard(str(u.get("pose") or ""))
    if pneg:
        neg = (neg + ", " + pneg) if neg else pneg
        u["pose_negative_added"] = pnote

    # 窄景别 + 选了袜类/鞋 → 腿脚不在画面，黑丝和鞋白选
    ln = str(u.get("legwear") or "")
    fn = str(u.get("footwear") or "")
    if (ln and ln not in ("（不写）", "（未成年类别已自动清空）")) or \
       (fn and fn not in ("（不写）",)):
        frame_map = (_REAL_POOLS.get("pose_framing") or {}).get("bundle_frame") or {}
        hit = next((f for n, f in frame_map.items() if n and n in text), "")
        if hit in ("close", "mid"):
            u["framing_conflict"] = (
                "⚠️ 选了袜类/鞋，但景别是胸像或半身 —— 腿脚不在画面里，黑丝和鞋基本看不见。"
                "想让它们入镜请选「全身 / 七分身」类取景包。")

    plan["negative"] = neg

    # 提示词过长会开始漏项 —— 实测 500 字时「全身连体丝 + 手部包覆」被整段忽略，
    # 压到 110 字后立刻画出来了。给个长度预警，让用户主动关掉次要槽。
    if len(text) > 360:
        u["prompt_length_warning"] = (
            "⚠️ 提示词 %d 字，偏长。实测超过约 350 字后模型会开始漏项"
            "（例如全身连体丝被画成普通黑衣）。建议关掉帽子 / 包 / 首饰等次要槽再试。"
            % len(text))


def _frame_negative_for(prompt_text: str) -> tuple[str, str]:
    """从固定提示词里认出景别，返回 (要追加的负面, 命中的关键词)。"""
    for key, neg in FRAME_NEG_RULES:
        if key in (prompt_text or ""):
            return neg, key
    return "", ""


def _water_guard(prompt_text: str, lora: str) -> tuple[str, str]:
    """场景/提示词带水元素时，给出要追加的负面与说明。"""
    hit = next((w for w in WATER_WORDS if w in (prompt_text or "")), "")
    if not hit:
        return "", ""
    rain = "rainywindow" in (lora or "")
    note = ("命中水元素「%s」→ 已加负面压制「水珠跑到身上」" % hit)
    if rain:
        note += "（并且当前用了 krea2_rainywindow 雨窗 LoRA，它会主动强化水珠）"
    return WATER_NEG, note


def _tidy_prompt(text: str) -> str:
    """清理空槽留下的悬空标点。

    关掉某一项时模板会留下「她穿着。」「背景是，。」这类残渣 —— 必须清掉，
    否则 Krea2 会把残缺的句子当成有效描述。
    """
    text = re.sub(r"她穿着(?=[，。])", "", text)
    text = re.sub(r"背景是(?=[，。])", "", text)
    text = re.sub(r"[，,]{2,}", "，", text)
    text = re.sub(r"^[，。、]+", "", text)
    text = re.sub(r"，\s*。", "。", text)
    text = re.sub(r"。\s*。+", "。", text)
    text = re.sub(r"\s+", " ", text)
    # 去掉重复的短句段。踩到过：body 和 bust 两个槽都产出「曲线明显」，
    # 拼起来是「…腰臀比例突出，…，胸部丰满，E罩杯，曲线明显。」——
    # 同一属性被说两遍会放大它的权重，把构图往那个部位拉。
    out, seen = [], set()
    for seg in text.split("，"):
        key = seg.strip()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(seg)
    return "，".join(out).strip()


def draw_real(choice: dict, rng) -> dict:
    """按用户选择从各槽抽词，组装成中文散文提示词。

    每个槽的取值（所有槽统一）：
      'random' / 缺失 / 空串 -> 随机抽一条
      'off'                  -> 这一项不写
      其他                   -> 指定条目（需命中池内，否则退回随机并标注）

    服装两种模式（clothing_mode）：
      'set'   成套服装：按 clothing_category 抽整套（原有 54 套 / 9 类别）
      'split' 拆分搭配：11 个槽逐槽抽或指定，拼成一句中文散文
    两者互斥不叠加。cosplay 的「角色原装」优先级最高，会跳过服装抽取。
    """
    pools = _REAL_POOLS.get("pools", {})
    q = _REAL_POOLS.get("quality", {})
    templates = _REAL_POOLS.get("templates", {})
    rules = _REAL_POOLS.get("slot_rules", {})
    male_wardrobe = _REAL_POOLS.get("male_wardrobe") or {}
    person_gender = _REAL_POOLS.get("person_gender") or {}
    male_skip = set(_REAL_POOLS.get("male_skip_slots") or [])
    used: dict = {}
    # 下面两项在人物解析后赋值（性别决定服装池与主语）
    gender = "female"

    def pool_items(key):
        # 男性：发型 / 服装 / 鞋 / 包 / 首饰改用男装池。
        # 起因：加了男性人物分类却沿用女性服装池，实测出现「年轻男性+长卷发+黑色厚丝袜」。
        if gender == "male" and key in male_wardrobe:
            return list(male_wardrobe[key])
        pool = pools.get(key) or {}
        if "categories" in pool:
            return [t for arr in pool["categories"].values() for t in arr]
        return list(pool.get("items") or [])

    def resolve(key, safe=False):
        """统一槽解析：全部随机 / 不写 / 从勾选的子集里抽。返回最终文本。

        safe=True 时（且当前人物是未成年类别）会先过滤掉泳装/内衣/丝袜类条目。
        """
        items = pool_items(key)
        if safe and minor:
            filtered = [t for t in items if not _is_adult_item(t)]
            # ★ 即使过滤后为空，也只用过滤后的结果 —— 绝不回退到未过滤的池。
            #   过滤后为空说明这个槽整体不适合未成年类别，那就跳过。
            items = filtered
            if not items:
                used[key] = "（该槽没有适合未成年类别的条目，已跳过）"
                return ""
        if not items:
            return ""
        mode, sel = _slot_sel(choice, key)
        if mode == "off":
            used[key] = "（不写）"
            return ""
        if mode == "pick":
            valid = [x for x in sel if x in items]
            if not valid:
                # ★ 未成年安全过滤绝不能被「原样使用」绕过去。
                #   踩过：幼女 + 指定黑丝/比基尼 → 过滤已把它们从池里剔掉 →
                #   勾选值变成「不在池内」→ 走了原样使用分支 →
                #   提示照常显示「已自动排除」，实际一条都没拦住（比不提示更糟）。
                if safe and minor:
                    blocked = [x for x in (sel or []) if _is_adult_item(x)]
                    if blocked:
                        v = rng.choice(items)
                        used[key] = "%s（未成年类别不可用，已换成：%s）" % (blocked[0], v)
                        return v
                # 池里没有也原样用 —— 用户可能手写了自己的条目，或池子改版后条目改名。
                # 静默退回随机会让「我明明选了」变成「根本没生效」，而且界面上看不出来。
                v = rng.choice([x for x in sel if x]) if any(sel) else rng.choice(items)
                used[key] = "%s（不在池内，已原样使用）" % v
                return v
            v = rng.choice(valid)
            used[key] = v if len(valid) == 1 else "%s（从勾选的 %d 条里抽）" % (v, len(valid))
            return v
        v = rng.choice(items)
        used[key] = v
        return v

    def person_category(value):
        """反查某个人物条目属于哪个年龄/性别类别。"""
        cats = (pools.get("person") or {}).get("categories") or {}
        for name, arr in cats.items():
            if value and value in arr:
                return name
        return None

    # 人物必须先解析：抽到未成年类别要收紧服装与身材槽；性别决定服装池与主语
    person_val = resolve("person")
    person_cat = person_category(person_val)
    gender = person_gender.get(person_cat, "female")
    pronoun = "他" if gender == "male" else "她"
    coser = "一位二十多岁的年轻男性coser" if gender == "male" else "一位二十多岁的年轻女性coser"
    used["gender"] = "男性" if gender == "male" else "女性"
    minor = bool(person_cat) and person_cat in set(
        _REAL_POOLS.get("minor_person_categories") or [])
    if minor:
        used["minor_safety"] = MINOR_SAFE_NOTE % person_cat

    def pick_set_clothing():
        """成套服装：勾选的整套 > 勾选的类别 > 全类别随机。

        clothing_fixed / clothing_category 都支持多选白名单。
        """
        cats = (pools.get("clothing") or {}).get("categories") or {}
        if minor:
            # 未成年类别：整套池里先剔掉泳装/内衣/丝袜类条目
            cats = {c: [t for t in arr if not _is_adult_item(t)] for c, arr in cats.items()}
            cats = {c: arr for c, arr in cats.items() if arr}
        flat_all = [t for c, arr in cats.items() for t in arr]
        c2g = {t: c for c, arr in cats.items() for t in arr}

        fmode, fsel = _slot_sel(choice, "clothing_fixed")
        if fmode == "off":
            used["clothing"] = "（不写服装）"
            return ""
        if fmode == "pick":
            valid = [x for x in fsel if x in flat_all]
            if valid:
                v = rng.choice(valid)
                used["clothing"] = "成套·%s · %s" % (c2g.get(v, "?"), v) if len(valid) == 1 else \
                    "成套·%s · %s（从勾选的 %d 套里抽）" % (c2g.get(v, "?"), v, len(valid))
                return v
            used["clothing"] = "（勾选的成套服装都无效，改按类别抽）"

        cmode, csel = _slot_sel(choice, "clothing_category")
        if cmode == "off":
            used["clothing"] = "（不写）"
            return ""
        if cmode == "pick":
            picked_cats = [c for c in csel if c in cats]
            pool_ = [t for c in picked_cats for t in cats[c]]
            if pool_:
                v = rng.choice(pool_)
                used["clothing"] = "成套·%s · %s%s" % (
                    c2g.get(v, "?"), v,
                    "" if len(picked_cats) == 1 else "（从勾选的 %d 个类别里抽）" % len(picked_cats))
                return v
        if not flat_all:
            return ""
        c, v = rng.choice([(c, t) for c, arr in cats.items() for t in arr])
        used["clothing"] = "成套·%s · %s" % (c, v)
        return v

    def pick_camera(only_wide=False, min_frame=None):
        """返回 (取景文本, 取景负面, 取景包名)。名字要给动作池做景别约束。

        only_wide：只要宽景别（选全身连体丝/鞋时用）
        min_frame：至少要多宽的景别（明确选了地面坐姿等需要露腿的姿势时用）
                   —— 否则会抽到「胸像·近景」把腿裁掉，姿势等于白选。
        """
        bundles = (pools.get("camera") or {}).get("bundles") or []
        _fm2 = (_REAL_POOLS.get("pose_framing") or {}).get("bundle_frame") or {}
        _rank = {"close": 0, "mid": 1, "wide": 2}
        if only_wide:
            _w = [x for x in bundles if _fm2.get(x.get("name")) == "wide"]
            if _w:
                bundles = _w
        elif min_frame:
            _need = _rank.get(min_frame, 2)
            _sel2 = [x for x in bundles
                     if _rank.get(_fm2.get(x.get("name"), "any"), 2) >= _need]
            if _sel2:
                bundles = _sel2
        if not bundles:
            return "", "", ""
        mode, sel = _slot_sel(choice, "camera_bundle")
        if mode == "off":
            used["camera"] = "（不写）"
            return "", "", ""
        if mode == "pick":
            valid = [b for b in bundles if b["name"] in sel]
            if valid:
                b = rng.choice(valid)
                used["camera"] = b["name"] if len(valid) == 1 else \
                    "%s（从勾选的 %d 个取景包里抽）" % (b["name"], len(valid))
                return b.get("text", ""), b.get("negative", ""), b["name"]
        b = rng.choice(bundles)
        used["camera"] = b["name"]
        return b.get("text", ""), b.get("negative", ""), b["name"]

    def scene_category(value):
        """反查某个场景条目属于哪个类别（用于光线配对）。"""
        cats = (pools.get("scene") or {}).get("categories") or {}
        for name, arr in cats.items():
            if value and value in arr:
                return name
        return None

    def resolve_light(scene_val):
        """光线：勾选了就听用户的；否则若场景已定，只在 scene_light_map 允许的类别里抽。

        场景与光线原本是两个独立池，会抽出「开满野花的草地 + 室内窗边的自然光」
        这种自相矛盾的组合。**只在「全部随机」时才按场景收窄** —— 用户
        显式勾选的光线一律照办，哪怕它和场景不搭。
        """
        items = pool_items("light")
        if not items:
            return ""
        mode, sel = _slot_sel(choice, "light")
        if mode == "off":
            used["light"] = "（不写）"
            return ""
        if mode == "pick":
            valid = [x for x in sel if x in items]
            if valid:
                v = rng.choice(valid)
                used["light"] = v if len(valid) == 1 else \
                    "%s（从勾选的 %d 条里抽）" % (v, len(valid))
                return v
            v = rng.choice(items)
            used["light"] = "%s（所选项都无效，已退回全部随机）" % v
            return v

        sc = scene_category(scene_val)
        allowed = []
        if sc:
            names = (_REAL_POOLS.get("scene_light_map") or {}).get(sc) or []
            lcats = (pools.get("light") or {}).get("categories") or {}
            allowed = [t for n in names for t in (lcats.get(n) or [])]
        if allowed:
            v = rng.choice(allowed)
            used["light"] = "%s（按场景「%s」配对）" % (v, sc)
            return v
        v = rng.choice(items)
        used["light"] = v
        return v

    def resolve_pose(bundle_name):
        """动作：只在「全部随机」时按景别收窄；勾选的动作一律照办。

        「胸像 · 近景」配「侧躺在沙发上」是自相矛盾的（和 scene×light 同类问题）。
        只给 9 个【类别】标最窄景别，不用逐条标 —— 维护成本低。
        """
        items = pool_items("pose")
        if not items:
            return ""
        mode, sel = _slot_sel(choice, "pose")
        if mode == "off":
            used["pose"] = "（不写）"
            return ""
        if mode == "pick":
            valid = [x for x in sel if x in items]
            if valid:
                v = rng.choice(valid)
                used["pose"] = v if len(valid) == 1 else \
                    "%s（从勾选的 %d 条里抽）" % (v, len(valid))
                return v
            v = rng.choice(items)
            used["pose"] = "%s（所选项都无效，已退回全部随机）" % v
            return v

        pf = _REAL_POOLS.get("pose_framing") or {}
        rank = {"close": 0, "mid": 1, "wide": 2}
        frame = (pf.get("bundle_frame") or {}).get(bundle_name) or "any"
        level = 2 if frame == "any" else rank.get(frame, 2)
        minf = pf.get("pose_min_frame") or {}
        cats = (pools.get("pose") or {}).get("categories") or {}
        allowed = [t for c, arr in cats.items()
                   for t in arr if rank.get(minf.get(c, "close"), 0) <= level]
        if allowed and len(allowed) < len(items):
            v = rng.choice(allowed)
            used["pose"] = "%s（按景别「%s」收窄：%d/%d 条可选）" % (
                v, bundle_name or "未指定", len(allowed), len(items))
            return v
        v = rng.choice(items)
        used["pose"] = v
        return v

    def resolve_character():
        """角色：勾选多个时每张图随机抽一个（角色库是 Danbooru tag 列表）。"""
        v = choice.get("character")
        if isinstance(v, (list, tuple, set)):
            opts = [str(x).strip() for x in v if str(x).strip()]
            if not opts:
                return ""
            c = rng.choice(opts)
            used["character"] = c if len(opts) == 1 else "%s（从勾选的 %d 个角色里抽）" % (c, len(opts))
            return c
        s = str(v or "").strip()
        if s:
            used["character"] = s
        return s

    # ---------- 服装 ----------
    is_cosplay = choice.get("mode") == "cosplay"
    # __character__ 只在 cosplay 模式下才成立；它现在是「类别多选」里的一个可勾项，
    # 所以要在勾选集合里找，而不是比较单个值。
    _cmode, _csel = _slot_sel(choice, "clothing_category")
    char_original = is_cosplay and _cmode == "pick" and "__character__" in (_csel or [])
    mode = choice.get("clothing_mode") or "set"

    # 防呆：成套控件里勾了具体项目、模式却是「拆分」，而拆分槽一个都没勾
    # —— 说明是模式残留（UI 版本错配，或用户忘了切模式）。
    # 真实踩到：cosplay 下勾了「泳装沙滩 + 碎花比基尼」，因为 clothing_mode 还是默认的
    # split，成套选择被整段忽略，还随机加了一身衣服 → 用户看到「凭空多出不知道的衣服」。
    # 用户明确勾过的东西绝不能悄悄丢掉，所以这里按成套处理，并把原因记进 used。
    if not char_original and mode == "split":
        split_picked = any(_slot_sel(choice, k)[0] == "pick" for k in SPLIT_SLOT_ORDER)
        set_picked = (_slot_sel(choice, "clothing_fixed")[0] == "pick"
                      or _slot_sel(choice, "clothing_category")[0] == "pick")
        if set_picked and not split_picked:
            mode = "set"
            used["clothing_mode_note"] = (
                "成套服装里勾了具体项目 → 按成套处理（拆分槽一个都没勾，模式可能是残留）"
            )

    clothing_set = ""
    outfit_text = ""

    if char_original:
        used["clothing_mode"] = "角色原装"
        used["clothing"] = "角色原装（不随机服装）"
    elif mode == "split":
        used["clothing_mode"] = "拆分搭配"
        used["clothing"] = "（拆分搭配，逐槽见下）"

        # 先定「轮廓」：连衣裙 与 上衣+下衣 在语义上互斥，不能各自独立随机。
        # 早先的写法是先各自抽再清空，结果随机抽中的连衣裙会把用户【明确勾选】的
        # 上衣/下衣静默吃掉（实测踩到：勾了「黑丝+皮短裤」被换成一条长裙）。
        def _explicit(k):
            m, _ = _slot_sel(choice, k)
            return m == "pick"

        if gender == "male":
            silhouette = "top_bottom"       # 男性不穿连衣裙
        elif _explicit("dress"):
            silhouette = "dress"
        elif _explicit("top") or _explicit("bottom"):
            silhouette = "top_bottom"
        else:
            silhouette = "dress" if rng.random() < DRESS_RATIO else "top_bottom"
        used["silhouette"] = "连衣裙" if silhouette == "dress" else "上衣+下衣"

        skip = list(("top", "bottom") if silhouette == "dress" else ("dress",))
        if gender == "male":
            skip += [k for k in male_skip if k not in skip]
        vals = {}
        for k in SPLIT_SLOT_ORDER:
            if k in skip:
                vals[k] = ""
                used[k] = "（本轮廓不使用）"
                continue
            # safe=True：未成年类别下过滤泳装/内衣/丝袜类条目
            vals[k] = resolve(k, safe=True)
        # 全身连体丝 = 整体服装，与上衣/下衣/连衣裙互斥（和「连衣裙抢上衣」同一类冲突）。
        # 实测抽到「真丝吊带睡裙上半身 + 黑色蕾丝内裤 + 全身连体黑丝」这种自相矛盾的组合。
        if "全身" in (vals.get("legwear") or ""):
            _cleared = []
            for _k in ("top", "bottom", "dress", "outerwear"):
                if vals.get(_k) and not _explicit(_k):
                    vals[_k] = ""
                    used[_k] = "（被全身连体丝取代，已从提示词删除）"
                    _cleared.append(_k)
            if _cleared:
                used["bodysuit_conflict"] = (
                    "袜类抽到「全身连体丝」→ 已自动清空 %s"
                    "（整体服装与分件上衣/下衣/裙互斥）" % "、".join(_cleared))
        clauses = [v for v in vals.values() if v]
        if clauses:
            outfit_text = pronoun + "，".join(clauses) + "。"
    elif gender == "male":
        # 成套服装那 54 套全是女性搭配 —— 男性自动改走拆分搭配的男装池
        mode = "split"          # 必须同步改 mode，否则模板选择仍按成套走 → 出现「他穿着。」
        used["clothing_mode"] = "拆分搭配（男性自动切换）"
        used["clothing_mode_note"] = "成套服装池是女性搭配，男性已自动改用拆分搭配的男装池"
        used["silhouette"] = "上衣+下衣"
        vals = {}
        for k in SPLIT_SLOT_ORDER:
            if k in male_skip:
                vals[k] = ""
                used[k] = "（男性不使用）"
                continue
            vals[k] = resolve(k, safe=True)
        clauses = [v for v in vals.values() if v]
        if clauses:
            outfit_text = pronoun + "，".join(clauses) + "。"
    else:
        used["clothing_mode"] = "成套服装"
        clothing_set = pick_set_clothing()

    # ---------- 其余槽 ----------
    # 镜头必须先解析：景别会约束动作池。
    # scene 必须先于 light：光线要按场景配对收窄。
    # 明确选了「全身连体丝」或鞋 → 强制宽景别。
    # 否则取景随机到半身/胸像时，黑丝和鞋根本不在画面（用户实测踩到）。
    _lm, _lsel = _slot_sel(choice, "legwear")
    _fm, _fsel = _slot_sel(choice, "footwear")
    force_wide = ((_lm == "pick" and any("全身" in x for x in (_lsel or [])))
                  or (_fm == "pick" and bool(_fsel)))
    # 明确选了某个姿势 → 取景必须容得下它（地面坐姿需要 wide，椅类至少 mid），
    # 否则会抽到「胸像·近景」把腿裁掉，用户看到的姿势和选的不一样。
    _need_frame = None
    _pm, _psel = _slot_sel(choice, "pose")
    if _pm == "pick" and _psel:
        _pcats = (pools.get("pose") or {}).get("categories") or {}
        _minf = (_REAL_POOLS.get("pose_framing") or {}).get("pose_min_frame") or {}
        for _x in _psel:
            for _cn, _arr in _pcats.items():
                if _x in _arr:
                    _fr = _minf.get(_cn)
                    if _fr == "wide":
                        _need_frame = "wide"
                    elif _fr == "mid" and _need_frame != "wide":
                        _need_frame = "mid"
    cam_text, cam_neg, cam_name = pick_camera(only_wide=force_wide, min_frame=_need_frame)
    if force_wide:
        used["framing_forced"] = (
            "明确选了全身连体丝或鞋 → 已强制使用宽景别取景包「%s」，保证手脚入镜" % cam_name)
    elif _need_frame:
        used["framing_forced"] = (
            "明确选的姿势至少需要「%s」景别 → 已收窄取景包到「%s」" % (_need_frame, cam_name))
    parts = {"person": person_val}
    for k in BASE_SLOTS:
        if k == "person":
            continue                      # 已在前面解析（未成年判定要用）
        if k == "body":
            if minor:
                parts[k] = ""
                used[k] = "（未成年类别已自动清空）"
            else:
                parts[k] = resolve(k)
        elif k == "light":
            parts[k] = resolve_light(parts.get("scene", ""))
        elif k == "pose":
            parts[k] = resolve_pose(cam_name)
        else:
            parts[k] = resolve(k)

    # 身材细分（身高/胸/臀/大腿）→ 拼成 {body_extra}，跟在 {body} 后面
    detail = []
    for k in (_REAL_POOLS.get("body_detail_slots") or BODY_DETAIL_SLOTS):
        if minor:
            parts[k] = ""
            used[k] = "（未成年类别已自动清空）"
            continue
        v = resolve(k)
        parts[k] = v
        if v:
            detail.append(v)
    parts["body_extra"] = ("，" + "，".join(detail)) if detail else ""

    # 身材描述在窄景别下根本不在画面里 —— 明确提示，免得用户以为「选了没用」。
    # （实测：用户反馈身材选项没效果，查下来是选择被正确采纳了，但取景包是胸像/半身。）
    if parts.get("body"):
        _pf = _REAL_POOLS.get("pose_framing") or {}
        _frame = (_pf.get("bundle_frame") or {}).get(cam_name)
        if _frame in ("close", "mid"):
            used["body_visibility"] = (
                "⚠️ 取景包「%s」是%s景别，身材基本不在画面里 —— 这条身材描述接近白写；"
                "想看身材请选「全身 / 七分身」类取景包"
                % (cam_name, "胸像" if _frame == "close" else "半身"))

    parts["clothing"] = clothing_set
    parts["outfit"] = outfit_text
    parts["quality"] = q.get("positive", "")
    parts["character"] = resolve_character()
    parts["pronoun"] = pronoun        # 她 / 他 —— 模板里不再硬编码
    parts["coser"] = coser            # 女性 / 男性 coser
    parts["camera"] = cam_text

    # ---------- 选模板 ----------
    if char_original:
        tpl_name = "cosplay_nocloth"
    elif mode == "split" and outfit_text:
        tpl_name = "cosplay_split" if is_cosplay else "normal_split"
    else:
        tpl_name = "cosplay" if is_cosplay else "normal"
    tpl = templates.get(tpl_name) or templates.get("normal") or "{person}"
    used["template"] = tpl_name

    text = tpl
    for k, v in parts.items():
        text = text.replace("{" + k + "}", str(v or ""))
    text = _tidy_prompt(text)

    neg = q.get("negative", "")
    if cam_neg:
        neg = (neg + ", " + cam_neg) if neg else cam_neg
    return {"prompt": text, "negative": neg, "used": used, "template": tpl_name}


# ============================================================================
# 任务管理
# ============================================================================
class Job:
    __slots__ = ("id", "kind", "status", "prompt_id", "result", "error", "started",
                 "note", "cancel")

    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.status = "queued"
        self.prompt_id = None
        self.result = None
        self.error = None
        self.started = time.time()
        self.note = ""
        self.cancel = False        # 用户点了「停止」

    def snapshot(self) -> dict:
        return {"id": self.id, "kind": self.kind, "status": self.status,
                "elapsed": round(time.time() - self.started, 1),
                "result": self.result, "error": self.error, "note": self.note,
                "cancel": self.cancel}


JOBS: dict[str, Job] = {}
COMFY: Comfy | None = None


async def _await_outputs(pid: str, timeout: float = 1800.0, job: Job | None = None):
    """等一个 ComfyUI 任务跑完，返回 (ok, images, texts, error)。

    传 job 时会盯 job.cancel：用户点「停止」后立刻返回，不必等超时。
    """
    assert COMFY is not None
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(1.5)
        if job is not None and job.cancel:
            await COMFY.interrupt()
            return False, [], [], "已手动停止"
        rec = await COMFY.history(pid)
        if not rec:
            continue
        st = rec.get("status") or {}
        if st.get("status_str") == "error" or not st.get("completed", True):
            msgs = st.get("messages") or []
            err = "执行出错"
            for m in msgs:
                if isinstance(m, list) and m and m[0] == "execution_error":
                    info = m[1] if len(m) > 1 else {}
                    err = "%s: %s" % (info.get("node_type"), info.get("exception_message"))
                    break
            return False, [], [], err
        outputs = rec.get("outputs") or {}
        images, texts = [], []
        for nid, o in outputs.items():
            for im in (o.get("images") or []):
                if im.get("type") == "temp":
                    continue
                images.append({"node": nid, "filename": im.get("filename"),
                               "subfolder": im.get("subfolder") or "",
                               "type": im.get("type") or "output"})
            for key in ("text", "string", "value"):
                v = o.get(key)
                if isinstance(v, list):
                    texts.extend(str(x) for x in v)
                elif isinstance(v, str):
                    texts.append(v)
        # 同一段文本可能被 PreviewAny 和 SaveText 各报一次，去重（保序）
        seen: set[str] = set()
        uniq: list[str] = []
        for t in texts:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        return True, images, uniq, None
    return False, [], [], "超时（30 分钟）"


async def run_job(job: Job, graph: dict):
    """提交 -> 轮询 -> 收集产出（单套提示词）"""
    assert COMFY is not None
    try:
        job.status = "running"
        job.prompt_id = await COMFY.submit(graph)
        ok, images, texts, err = await _await_outputs(job.prompt_id)
        if not ok:
            job.status, job.error = "error", err
            return
        # 合并而不是覆盖：提交时写进去的 plans/seed 要保留，
        # 否则 /api/job/{id} 完成后就拿不到「这张图用的哪套提示词」了。
        merged = dict(job.result or {})
        merged.update({"images": images, "texts": texts})
        job.result = merged
        job.status = "done"
    except Exception as e:
        job.status, job.error = "error", "%s: %s" % (type(e).__name__, str(e)[:400])


async def run_job_batch(job: Job, plans: list, common: dict, base_seed: int):
    """每张图各用【自己那套提示词】—— 一张一个 ComfyUI 任务，按序收集。

    旧做法是 batch>1 时把同一套词灌进一个 batch，等于抽一次卡画 N 张变体，
    19 个槽的随机性全被浪费 —— 用户看到的是「抽 3 次出 3 张几乎一样的图」。
    """
    assert COMFY is not None
    try:
        job.status = "running"
        all_images: list = []
        all_texts: list = []
        for i, plan in enumerate(plans):
            if job.cancel:                      # 上一张期间点了停止
                job.status = "cancelled"
                job.error = "已手动停止（完成 %d/%d 张）" % (i, len(plans))
                return
            graph = wf_real(prompt=plan["prompt"], negative=plan["negative"],
                            seed=base_seed + i, batch=1, **common)
            pid = await COMFY.submit(graph)
            job.prompt_id = pid
            ok, images, texts, err = await _await_outputs(pid, job=job)
            if not ok:
                if job.cancel:                  # 点在生成途中
                    job.status = "cancelled"
                    job.error = "已手动停止（完成 %d/%d 张）" % (i, len(plans))
                    merged = dict(job.result or {})
                    merged.update({"images": list(all_images), "texts": all_texts,
                                   "done_count": i, "count": len(plans)})
                    job.result = merged
                    return
                job.status = "error"
                job.error = "第 %d/%d 张失败：%s" % (i + 1, len(plans), err)
                return
            for im in images:
                im["index"] = i
                im["prompt"] = plan["prompt"]
            all_images.extend(images)
            all_texts.extend(texts)
            merged = dict(job.result or {})
            merged.update({"images": list(all_images), "texts": all_texts,
                           "done_count": i + 1, "count": len(plans)})
            job.result = merged
        job.status = "done"
    except Exception as e:
        job.status, job.error = "error", "%s: %s" % (type(e).__name__, str(e)[:400])


# ============================================================================
# HTTP 路由
# ============================================================================
routes = web.RouteTableDef()


@routes.get("/")
async def index(_: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC / "index.html")


@routes.get("/api/real/options")
async def api_real_options(_: web.Request) -> web.Response:
    return web.json_response(real_options())


@routes.post("/api/real/draw")
async def api_real_draw(request: web.Request) -> web.Response:
    """只抽词看提示词，不生成 —— 用来确认拼装是否通顺。

    必须和 /api/real 走同一套负面兜底，否则界面看到的负面和真正生成用的不一致
    （踩过：预览的负面没有平胸项，生成时才有）。
    """
    b = await request.json()
    rng = random.Random(b.get("seed") if b.get("seed") not in (None, -1) else None)
    plan = draw_real(b, rng)
    _apply_plan_guards(plan, str(b.get("lora") or ""))
    return web.json_response(plan)


@routes.post("/api/real")
async def api_real(request: web.Request) -> web.Response:
    assert COMFY is not None
    b = await request.json()
    batch = max(1, min(20, _as_int(b.get("count"), 1)))
    seed = _as_int(b.get("seed"), -1)
    base_seed = seed if seed not in (None, -1) else random.randint(0, 2 ** 31 - 1)

    # ① 固定提示词模式：前端把文本框里的提示词直接发过来，就原样使用、不重新抽词。
    #    用途：抽一次词 → 手动改改（比如换个衣服颜色）→ 用同一套词连出几张。
    #    每张图仍然换种子，所以是同一套词的 N 个变体（等于原来的生图功能）。
    fixed_prompt = str(b.get("prompt") or "").strip()
    if fixed_prompt:
        fixed_neg = str(b.get("negative") or "").strip()
        # 负面为空时补上默认负面。踩到过：固定提示词模式下前端没抽过词，
        # negative 文本框是空的，于是整条负面约束（模糊/低质量/塑料感…）全丢了，
        # 出图质量明显下降却看不出来 —— 空负面 ≠ 想禁用负面。
        _neg_note = ""
        if not fixed_neg:
            fixed_neg = (_REAL_POOLS.get("quality") or {}).get("negative", "")
            _neg_note = "文本框里负面提示词是空的 → 已自动补上默认负面（想真正留空请填一个空格以外的占位）"
        plans = [{"prompt": fixed_prompt, "negative": fixed_neg,
                  "template": "manual（固定提示词，未抽词）",
                  "used": {"source": "固定提示词 —— 未重新抽词，每张只换种子"}}
                 for _ in range(batch)]
        if _neg_note:
            for _p in plans:
                _p["used"]["negative_note"] = _neg_note
    else:
        # ② 抽词模式：每张图独立抽一次词，并把完整提示词记进任务里，方便回看
        plans = []
        for i in range(batch):
            rng = random.Random(None if seed in (None, -1) else base_seed + i)
            plans.append(draw_real(b, rng))

    # 每张图用它自己那套词（各提交一个 ComfyUI 任务）——
    # 不再把同一套词灌进 batch 出变体，那样「抽 N 次」得到的是 N 张近乎重复的图。
    # 尺寸：给了宽高比就按「宽高比 + 百万像素」算，否则用显式宽高。
    # 对齐到 8 的倍数在 resolve_size 里做（不对齐潜空间会报错）。
    if b.get("aspect"):
        _aw, _ah = resolve_size(str(b.get("aspect")), _as_float(b.get("megapixels"), 1.5))
    else:
        _aw, _ah = _as_int(b.get("width"), 1024), _as_int(b.get("height"), 1536)
    common = dict(
        width=_aw, height=_ah,
        steps=_as_int(b.get("steps"), 8),
        cfg=_as_float(b.get("cfg"), 1.5),
        sampler=b.get("sampler") or "er_sde",
        scheduler=b.get("scheduler") or "simple",
        unet=b.get("unet") or "krea2_turbo_int8_convrot.safetensors",
        clip=b.get("clip") or "qwen3vl_4b_fp8_scaled.safetensors",
        vae=b.get("vae") or "qwen_image_vae.safetensors",
        lora=b.get("lora") or "",
        lora_strength=_as_float(b.get("lora_strength"), 1.0),
        lora_clip=_as_float(b.get("lora_clip"), 0.0),
        # 多 LoRA 串联：[{"name":..., "strength":..., "clip":...}, ...]
        loras=b.get("loras") or None,
        sharpen=_as_float(b.get("sharpen"), 0.0),
        refine=_as_float(b.get("refine"), 0.0),
        refine_steps=_as_int(b.get("refine_steps"), 4),
        refine_scale=_as_float(b.get("refine_scale"), 1.0),
        upscale_model=b.get("upscale_model") or "4x_foolhardy_Remacri.safetensors",
        # 测试产物单独目录：助手做验证时传 test_output=true，不要污染用户产出
        filename_prefix=(P_TEST if b.get("test_output") else ""),
    )
    # LoRA 说明写进每一张的 used：强度、推荐强度、以及过高时的警告。
    # 这里不做静默钳制 —— 用户设了就按用户的来，但必须把风险说出来。
    # 统一成列表，单 LoRA 与多 LoRA 走同一套说明逻辑。
    # 踩过：多 LoRA 上线后这段还只读旧的 lora 字段，导致「抽词明细」显示"（不用）"，
    # 实际却挂了 3 个 —— 透明化面板显示错的比不显示更糟。
    _chain_info = []
    for _it in (common.get("loras") or []):
        if isinstance(_it, dict) and _it.get("name"):
            _chain_info.append((_it["name"], float(_it.get("strength") or 1.0),
                                float(_it.get("clip") or 0.0)))
    if not _chain_info and (common.get("lora") or ""):
        _chain_info.append((common["lora"], float(common.get("lora_strength") or 1.0),
                            float(common.get("lora_clip") or 0.0)))
    _warn = ""
    for _nm, _ls, _lc in _chain_info:
        _lp = LORA_PRESETS.get(_nm) or {}
        if _lp.get("max_ok") and _ls > _lp["max_ok"]:
            _warn += ("⚠️ %s 建议强度 ≤ %.2f，当前 %.2f —— 极可能过熟（彩虹色边 / 洗色 / 肌理溶解）"
                      % (_nm, _lp["max_ok"], _ls))
    _lora = _chain_info[0][0] if _chain_info else ""
    _ls = _chain_info[0][1] if _chain_info else 1.0
    _lp = LORA_PRESETS.get(_lora) or {}
    # 跨模型族的 LoRA：权重形状对不上，轻则无效重则污染画面
    _uf = UNET_FAMILY.get(common.get("unet") or "")
    for _nm, _ls2, _lc2 in _chain_info:
        _lf = (LORA_PRESETS.get(_nm) or {}).get("family")
        if _lf and _uf and _lf != _uf:
            _warn += ("⚠️ 跨模型族：%s 属于 %s 族，当前底模属于 %s 族 —— "
                      "跨族使用大概率无效，甚至污染画面。" % (_nm, _lf, _uf))
    for _p in plans:
        _u = _p.setdefault("used", {})
        _u["lora"] = (" → ".join("%s @ %s" % (n, s) for n, s, _c in _chain_info)
                      if _chain_info else "（不用）")
        if len(_chain_info) > 1:
            _u["lora_chain"] = "共 %d 个 LoRA 依次串联" % len(_chain_info)
        if _lp.get("note"):
            _u["lora_note"] = _lp["note"]
        if _lp.get("trigger"):
            _u["lora_trigger"] = "建议把触发词写进提示词：%s" % _lp["trigger"]
        if _warn:
            _u["lora_warning"] = _warn
        _u["sharpen"] = ("%.2f" % common.get("sharpen") if common.get("sharpen") else "（关）")



    for _p in plans:
        _apply_plan_guards(_p, _lora)

    job = Job("real")
    JOBS[job.id] = job
    job.note = "\n\n".join("第%d张：%s" % (i + 1, p["prompt"]) for i, p in enumerate(plans))
    job.result = {"plans": plans, "seed": base_seed, "count": batch, "done_count": 0}
    asyncio.create_task(run_job_batch(job, plans, common, base_seed))
    return web.json_response(job.snapshot(), status=201)


@routes.get("/api/output-info")
async def api_output_info(_: web.Request) -> web.Response:
    """产物目录（给界面显示用）。用户反馈过「下载的文件不知道去哪找」。"""
    ok = COMFY_OUTPUT.is_dir()
    return web.json_response({
        "root": str(COMFY_OUTPUT),
        "exists": ok,
        "subdirs": {k: {"label": v[0], "rel": v[1],
                        "abs": str(COMFY_OUTPUT / v[1])}
                    for k, v in OUT_SUBDIRS.items()},
    })


@routes.post("/api/open-folder")
async def api_open_folder(request: web.Request) -> web.Response:
    """在资源管理器里打开产物目录。只允许打开白名单里的四个子目录。"""
    try:
        b = await request.json()
    except Exception:
        b = {}
    which = b.get("which") or "real"
    if which == "root":
        target = COMFY_OUTPUT
    elif which in OUT_SUBDIRS:
        target = COMFY_OUTPUT / OUT_SUBDIRS[which][1]
    else:
        return web.json_response({"ok": False, "error": "不允许的目录: %s" % which},
                                 status=400)
    if not target.is_dir():
        return web.json_response(
            {"ok": False, "error": "目录还不存在（这个工具还没出过图）: %s" % target},
            status=404)
    try:
        subprocess.Popen(["explorer", str(target)])
    except Exception as e:
        return web.json_response({"ok": False, "error": "%s: %s" % (type(e).__name__, e)},
                                 status=500)
    return web.json_response({"ok": True, "path": str(target)})


@routes.get("/api/status")
async def api_status(_: web.Request) -> web.Response:
    assert COMFY is not None
    return web.json_response(await COMFY.status())


@routes.get("/api/options")
async def api_options(_: web.Request) -> web.Response:
    assert COMFY is not None
    oi = await COMFY.object_info()
    return web.json_response({
        "upscalers": COMFY.combo(oi, "UpscaleModelLoader", "model_name"),
        "patches": COMFY.combo(oi, "ModelPatchLoader", "name"),
        "unets": COMFY.combo(oi, "UNETLoader", "unet_name"),
        "clips": COMFY.combo(oi, "CLIPLoader", "clip_name"),
        "vaes": COMFY.combo(oi, "VAELoader", "vae_name"),
        "loras": COMFY.combo(oi, "LoraLoaderModelOnly", "lora_name"),
        "samplers": COMFY.combo(oi, "KSampler", "sampler_name"),
        "schedulers": COMFY.combo(oi, "KSampler", "scheduler"),
    })


@routes.post("/api/upload")
async def api_upload(request: web.Request) -> web.Response:
    assert COMFY is not None
    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "file":
        raise web.HTTPBadRequest(text="缺少 file 字段")
    content = await field.read(decode=False)
    if not content:
        raise web.HTTPBadRequest(text="空文件")
    name = field.filename or "upload.png"
    safe = "".join(c for c in name if c.isalnum() or c in "._-") or "upload.png"
    stored = await COMFY.upload(safe, content)
    return web.json_response({"image": stored})


@routes.post("/api/pose")
async def api_pose(request: web.Request) -> web.Response:
    assert COMFY is not None
    b = await request.json()
    need = ("image", "prompt")
    if not all(b.get(k) for k in need):
        raise web.HTTPBadRequest(text="缺少 image 或 prompt")
    graph = wf_pose(
        image=b["image"], prompt=b["prompt"], negative=b.get("negative") or "",
        patch=b.get("patch") or "anima-lllite-pose-1.safetensors",
        strength=float(b.get("strength") or 1.0),
        width=int(b.get("width") or 832), height=int(b.get("height") or 1216),
        steps=int(b.get("steps") or 30), cfg=float(b.get("cfg") or 4.5),
        seed=int(b.get("seed") if b.get("seed") is not None else -1),
        sampler=b.get("sampler") or "dpmpp_2m_sde_gpu",
        scheduler=b.get("scheduler") or "beta",
        unet=b.get("unet") or "anima-base-v1.0.safetensors",
        clip=b.get("clip") or "qwen_3_06b_base.safetensors",
        vae=b.get("vae") or "qwen_image_vae.safetensors",
        lora=b.get("lora") or "", lora_strength=float(b.get("lora_strength") or 1.0),
        extract=bool(b.get("extract", True)),
        extract_mode=b.get("extract_mode") or "Full",
    )
    if graph["13"]["inputs"]["seed"] < 0:
        import random
        graph["13"]["inputs"]["seed"] = random.randint(0, 2 ** 31 - 1)
    job = Job("pose")
    JOBS[job.id] = job
    asyncio.create_task(run_job(job, graph))
    return web.json_response(job.snapshot(), status=201)


@routes.post("/api/upscale")
async def api_upscale(request: web.Request) -> web.Response:
    assert COMFY is not None
    b = await request.json()
    if not b.get("image"):
        raise web.HTTPBadRequest(text="缺少 image")
    graph = wf_upscale(
        image=b["image"],
        model=b.get("model") or "4x_foolhardy_Remacri.safetensors",
        mode=b.get("mode") or "scale",
        value=float(b.get("value") or 4.0),
        width=int(b.get("width") or 0), height=int(b.get("height") or 0),
    )
    job = Job("upscale")
    JOBS[job.id] = job
    asyncio.create_task(run_job(job, graph))
    return web.json_response(job.snapshot(), status=201)


@routes.post("/api/tag")
async def api_tag(request: web.Request) -> web.Response:
    assert COMFY is not None
    b = await request.json()
    if not b.get("image"):
        raise web.HTTPBadRequest(text="缺少 image")
    mode = b.get("mode") or "anime"
    if mode == "real":
        graph = wf_tag_real(image=b["image"],
                            clip=b.get("clip") or "qwen3vl_4b_fp8_scaled.safetensors",
                            max_length=int(b.get("max_length") or 700))
    else:
        graph = wf_tag_anime(image=b["image"],
                             general_threshold=float(b.get("general_threshold") or 0.35),
                             character_threshold=float(b.get("character_threshold") or 0.85))
    job = Job("tag")
    JOBS[job.id] = job
    asyncio.create_task(run_job(job, graph))
    return web.json_response(job.snapshot(), status=201)


@routes.get("/api/job/{job_id}")
async def api_job(request: web.Request) -> web.Response:
    job = JOBS.get(request.match_info["job_id"])
    if job is None:
        raise web.HTTPNotFound(text="任务不存在")
    return web.json_response(job.snapshot())


@routes.post("/api/job/{job_id}/cancel")
async def api_cancel(request: web.Request) -> web.Response:
    """停止生成：打断正在跑的这张，并清掉排队中的。

    用户反馈「生成任务时没有关闭选项，现在完全不能停止生成」——
    之前只有一个几乎点不到的接口，界面上没有任何按钮，且 interrupt 之后
    排队中的任务还会继续跑。
    """
    job = JOBS.get(request.match_info["job_id"])
    assert COMFY is not None
    if job and job.status in ("queued", "running"):
        job.cancel = True                 # 让 run_job_batch 的循环也能看到
        await COMFY.interrupt()
        await COMFY.clear_queue()
        if job.status == "queued":
            job.status = "cancelled"
            job.error = "已手动停止"
    return web.json_response(job.snapshot() if job else {})


@routes.get("/api/view")
async def api_view(request: web.Request) -> web.Response:
    assert COMFY is not None
    data, ctype = await COMFY.view(
        request.query.get("filename", ""),
        request.query.get("subfolder", ""),
        request.query.get("type", "output"))
    return web.Response(body=data, content_type=ctype,
                        headers={"Cache-Control": "no-cache"})


@routes.get("/api/download")
async def api_download(request: web.Request) -> web.Response:
    assert COMFY is not None
    fn = request.query.get("filename", "image.png")
    data, ctype = await COMFY.view(fn, request.query.get("subfolder", ""),
                                   request.query.get("type", "output"))
    return web.Response(body=data, content_type=ctype,
                        headers={"Content-Disposition": 'attachment; filename="%s"' % fn})


# ============================================================================
# 入口
# ============================================================================
async def on_shutdown(app: web.Application):
    if COMFY:
        await COMFY.close()


@web.middleware
async def no_cache_frontend(request: web.Request, handler):
    """前端资源一律不许缓存。

    踩过的坑：index.html 与 app.js 的修改时间会差十几分钟，而这套静态资源
    原本没有任何 Cache-Control，浏览器按「启发式缓存」各自决定寿命 ——
    结果拿到【新的 HTML + 旧的 app.js】，表现为「卡片标题是新的，里面的
    控件全空」。硬刷新能临时解决，但每次改前端都会再踩一次，所以在服务端根治。
    """
    resp = await handler(request)
    if request.path == "/" or request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


def main():
    global COMFY, COMFY_OUTPUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8191)
    ap.add_argument("--comfy-url", default="http://127.0.0.1:8188")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--comfy-output", default=str(COMFY_OUTPUT),
                    help="ComfyUI 的输出根目录（界面上会显示、并可一键打开）")
    a = ap.parse_args()

    try:
        COMFY_OUTPUT = Path(a.comfy_output)
    except Exception:
        pass

    COMFY = Comfy(a.comfy_url)
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    load_real_data()
    _n = sum(len(v.get("items") or []) +
             sum(len(x) for x in (v.get("categories") or {}).values()) +
             len(v.get("bundles") or [])
             for v in (_REAL_POOLS.get("pools") or {}).values())
    _c = sum(len(v) for v in _REAL_CHARS.values())

    app = web.Application(client_max_size=128 * 1024 * 1024,
                          middlewares=[no_cache_frontend])
    app.add_routes(routes)
    app.router.add_static("/static", STATIC)
    app.on_shutdown.append(on_shutdown)

    print("=" * 64)
    print("界面 B · ComfyUI 工具集")
    print("  访问地址 : http://%s:%d" % (a.host, a.port))
    print("  ComfyUI  : %s" % a.comfy_url)
    print("  工具     : 骨架生成 / 图变高清 / 图解析Tag / 真人随机生图")
    print("  真人池   : %d 条提示词，%d 个 cosplay 角色" % (_n, _c))
    print("=" * 64)

    if not a.no_browser:
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open("http://%s:%d" % (a.host, a.port))).start()

    web.run_app(app, host=a.host, port=a.port, print=None)


if __name__ == "__main__":
    main()
