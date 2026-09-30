
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field

METRICS_FILE = os.path.join(os.path.dirname(__file__), '..', 'logs', 'model_metrics.json')

@dataclass
class ModelMetrics:
    tokens_per_second: list[float] = field(default_factory=list)
    average_latency_ms: list[int] = field(default_factory=list)
    total_requests: int = 0
    total_tokens: int = 0

    def calculate_averages(self):
        avg_tps = sum(self.tokens_per_second) / len(self.tokens_per_second) if self.tokens_per_second else 0
        avg_latency = sum(self.average_latency_ms) / len(self.average_latency_ms) if self.average_latency_ms else 0
        return avg_tps, avg_latency

_metrics: defaultdict[str, ModelMetrics] = defaultdict(ModelMetrics)

def save_metrics():
    """Saves metrics to the specified JSON file."""
    os.makedirs(os.path.dirname(METRICS_FILE), exist_ok=True)
    with open(METRICS_FILE, 'w') as f:
        json.dump({model: metrics.__dict__ for model, metrics in _metrics.items()}, f, indent=4)

def load_metrics():
    """Loads metrics from the specified JSON file."""
    if not os.path.exists(METRICS_FILE):
        return
    with open(METRICS_FILE, 'r') as f:
        data = json.load(f)
        for model_name, metrics_data in data.items():
            _metrics[model_name] = ModelMetrics(**metrics_data)

def get_metrics(model_name: str) -> ModelMetrics:
    """Returns the metrics for a given model."""
    return _metrics[model_name]

def update_metrics(model_name: str, tps: float, latency_ms: int, tokens: int):
    """Updates the metrics for a given model."""
    metrics = get_metrics(model_name)
    metrics.tokens_per_second.append(tps)
    metrics.average_latency_ms.append(latency_ms)
    metrics.total_requests += 1
    metrics.total_tokens += tokens
    save_metrics()

def record_model_completion(model_name: str, latency_ms: float, input_tokens: int, output_tokens: int, duration_seconds: float):
    """Record completion metrics for a model (called by providers)."""
    total_tokens = input_tokens + output_tokens
    tps = total_tokens / duration_seconds if duration_seconds > 0 else 0
    update_metrics(model_name, tps, int(latency_ms), total_tokens)

# Load existing metrics on module import
load_metrics()
