import os
import tempfile

# 必须在导入 deep_research 之前设置，隔离运行时数据目录
os.environ["DR_DATA_DIR"] = tempfile.mkdtemp(prefix="dr_test_")
os.environ.pop("TAVILY_API_KEY", None)

import pytest  # noqa: E402

from deep_research.llm import set_llm_factory  # noqa: E402
from deep_research.tools.search import MockBackend, set_search_backend  # noqa: E402

from .fakes import FakeWorld  # noqa: E402


@pytest.fixture
def world():
    w = FakeWorld()
    set_llm_factory(lambda node, settings: w.model)
    set_search_backend(MockBackend())
    yield w
    set_llm_factory(None)
    set_search_backend(None)
