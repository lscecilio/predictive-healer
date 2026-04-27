# predictive-healer

Predicts service degradation from Prometheus metrics and triggers self-healing actions before users notice.

## Features
- Pulls last 1h of metrics
- Linear regression on CPU/memory/error-rate
- Triggers `systemctl restart` if predicted threshold breach in next 15min

## Usage
```bash
python healer.py --target=atlas-fiscal --threshold=0.8
```

## License
MIT
