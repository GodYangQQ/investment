# 直播内容 → 文字 → AI 分析（方法论 / Prompt）

本文件记录一套可复用的流水线：把 YouTube 财经直播转成带时间戳的文字，再由 AI（直接阅读文字，不额外调用 LLM 做总结）整理观点、顶底判断、提及的股票等。

适用对象示例：私募一哥常士杉（口音较重，需结合财经语境做合理推断）。

---

## 0. 原则

1. **转写优先**：先把音频高保真转成带时间戳的中文文字，这是所有分析的基础。
2. **不脑补、不造数**：ASR 出现奇怪组合时，结合上下文推断，但必须**标注“疑似/不确定”**；无法判断就留空，不硬编。
3. **带时间戳**：每条结论尽量能回指原文时间点，方便核对。
4. **增量处理**：记录已处理的 `video_id`，避免重复下载/转写。

---

## 1. 环境准备

```bash
# 下载器
python -m pip install --user -U yt-dlp

# 转写
python -m pip install --user faster-whisper

# 系统依赖：ffmpeg
#   Ubuntu: sudo apt install ffmpeg
#   macOS:  brew install ffmpeg
```

模型：
- 有 NVIDIA GPU：优先 `large-v3`（对中文口音最稳），`device=cuda, compute_type=float16`。
- GPU 缺 cuDNN 时：`pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`，并把其 lib 目录加入 `LD_LIBRARY_PATH`；否则回退 CPU。
- 无 GPU：CPU `medium`/`small`，`compute_type=int8`。
- 网络慢：可直接用 curl 下载 `Systran/faster-whisper-{model}` 的 `config.json / model.bin / tokenizer.json / vocabulary.txt` 到本地目录，再把本地目录路径传给 `WhisperModel`。

---

## 2. 拉取频道 / 直播列表

```bash
# 频道主页视频（注意：YouTube 解析可能需要 IPv4）
yt-dlp -4 --flat-playlist --playlist-end 40 \
  --print "%(id)s | %(duration)s | %(title)s" \
  "https://www.youtube.com/@CHANNEL_HANDLE/videos"

# 直播回放（正片通常在这里，1 小时左右）
yt-dlp -4 --flat-playlist --playlist-end 20 \
  --print "%(id)s | %(duration)s | %(title)s" \
  "https://www.youtube.com/@CHANNEL_HANDLE/streams"
```

- 若报 `Network is unreachable` / DNS 失败：加 `-4` 强制 IPv4。
- 需要登录态：加 `--cookies-from-browser chrome`。

---

## 3. 下载音频

```bash
yt-dlp -4 -x --audio-format m4a -f bestaudio \
  -o "audio/%(id)s.%(ext)s" \
  "https://www.youtube.com/watch?v=VIDEO_ID"
```

---

## 4. 转写（关键）

使用本仓库脚本：

```bash
python chang_informs/transcribe.py audio/<id>.m4a \
  -o transcripts/<id> \
  -m large-v3 -d cuda -c float16
```

脚本会输出：
- `<id>.txt`：`[start-end] 文本`
- `<id>.json`：`[{start, end, text}, ...]`

推荐参数（口音重时）：
- `language="zh"`
- `vad_filter=True`（切掉静音/呼吸声）
- `beam_size=5`
- `condition_on_previous_text=False`（减少口音导致的“复读/幻觉”循环）

长直播（1 小时+）：
- 先 `ffmpeg` 按 10–15 分钟切片并行转写，再拼接；
- 或只转写信息密度高的片段。

---

## 5. 口音/错字处理规则

ASR 常见错误类型：
- 股票名、人名、代码被听成同音词；
- 数字/点位（如“三千五百点”“3500”）错位；
- 财经术语连读（如“量能”“换手”“支撑压力”）。

处理方式：
1. 维护**纠错词典**：常见股票名/代码/术语 → 正确写法。
2. 结合上下文推断：例如“宁德”“宁王”“300750”大概率指宁德时代；“创业”“创指”指创业板指。
3. **不确定的用 `〔?〕` 标注**，不要伪装成确定事实。
4. 时间/价格类数字，若与上下文矛盾，标注“疑似”，不作为结论依据。

---

## 6. AI 分析输出框架

对整场直播（或指定片段）输出以下结构：

```json
{
  "meta": {"video_id": "", "title": "", "date": "", "duration": ""},
  "market_view": {
    "stance": "多 / 空 / 震荡",
    "confidence": "高/中/低",
    "summary": "",
    "evidence": [{"text": "", "ts": 0.0}]
  },
  "index_levels": [
    {"index": "上证/深成/创业板/科创50", "view": "", "support": null, "resistance": null,
     "top_bottom": "顶/底/震荡/无", "evidence_ts": 0.0}
  ],
  "stocks": [
    {"name": "", "code": "", "view": "看多/看空/观察", "reason": "",
     "price_levels": "", "evidence_ts": 0.0}
  ],
  "sectors": [
    {"name": "", "view": "看好/看空/中性", "reason": "", "evidence_ts": 0.0}
  ],
  "action": {"position": "", "risk": "", "time_horizon": ""},
  "risk_flags": [""],
  "uncertain": [{"text": "", "ts": 0.0, "note": "ASR 疑似错误"}]
}
```

要求：
- 每条观点尽量附原文引用 + 时间戳。
- 区分“明确观点”与“随口提及”。
- 若整场没有明确顶底判断，`top_bottom` 填“无”，不要硬凑。

---

## 7. 建议目录结构

```
chang_informs/
├── PROMPT.md            # 本文件
├── transcribe.py        # 转写脚本
├── audio/               # 下载的音频
├── transcripts/         # 转写结果 .txt/.json
├── models/              # 本地模型（可选）
└── reports/             # AI 分析结果
```

---

## 8. 已知坑

- YouTube 需要 IPv4：`yt-dlp -4`。
- HF 模型下载慢/断：用 curl 断点续传（`-C -`），或直接下到本地目录再加载。
- GPU 缺 cuDNN 时 ctranslate2 会报错，回退 CPU int8。
- 口音重 + 长直播：优先 `large-v3`，并接受少量错字，靠上下文校正与标注。
- 仅个人研究使用，不二次分发原内容。
