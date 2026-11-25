"""读取项目语料，并提供对标签认功能的便捷入口。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from accreditation import (  # noqa: E402
    AccreditationOffice,
    AccreditationService,
    evaluate_standard,
    parse_comment,
    parse_standard_text,
    process_round,
)


def load_corpus():
    """读取原样保存的语料条目。"""
    return json.loads((ROOT / "data" / "corpus.json").read_text(encoding="utf-8"))


def build_corpus_service(learner_name: str = "林霁") -> AccreditationService:
    """创建服务并导入 data/corpus.json 中的标准、评语和历史签认。"""
    service = AccreditationService(learner_name=learner_name)
    service.ingest_corpus(load_corpus())
    return service


__all__ = [
    "load_corpus",
    "build_corpus_service",
    "process_round",
    "AccreditationService",
    "AccreditationOffice",
    "parse_comment",
    "parse_standard_text",
    "evaluate_standard",
]
