# External 仓库清单

本目录用于存放从外部克隆的开源仓库，**不随主仓库提交**。
`.gitignore` 会忽略 `external/` 下的所有内容，仅保留本清单和 `.gitkeep`。

| 目录 | 用途 | Git 源 | 当前 commit | 体积 |
|---|---|---|---|---|
| `ai-berkshire` | AI Berkshire 投资研究框架（四大师分析 / 行业研究 / 组合管理） | https://github.com/xbtlin/ai-berkshire.git | `d9c124b` | ~172M |
| `serenity-skill` | Serenity 供应链卡点研究 skill（AI/半导体产业链瓶颈） | https://github.com/muxuuu/serenity-skill.git | `c2fe93d` | ~488K |
| `LangAlpha` | 多智能体 LLM 股票分析（LangGraph + Polygon/Yahoo/Tavily + Damodaran 估值） | https://github.com/Chen-zexi/LangAlpha.git | `3433910` | ~7.5M |

## 使用

```bash
cd external
git clone <url>
```

> 这些仓库只做本地参考/研究，不要把它们的 `.git` 或代码提交进主仓库。
> 如需把某个外部仓库的版本纳入主仓库，请改用 `git submodule add <url> external/<name>`。
