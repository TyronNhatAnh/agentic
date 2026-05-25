from .ba import run_ba
from .po import run_po
from .dev import run_dev
from .review import run_review

REGISTRY = {
    "ba": run_ba,
    "po": run_po,
    "dev": run_dev,
    "review": run_review,
}
