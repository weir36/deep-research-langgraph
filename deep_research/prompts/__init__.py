"""Prompt 模板加载（Jinja2）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


def render_prompt(name: str, **variables) -> str:
    variables.setdefault("CURRENT_TIME", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    variables.setdefault("locale", "zh-CN")
    return _env.get_template(f"{name}.md").render(**variables)
