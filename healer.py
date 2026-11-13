#!/usr/bin/env python3
# Atlas Predictive ML Healing v1.0 - M8
# Collects metrics, stores in predictive.metrics_history, runs LinearRegression
# to predict 24h ahead, fires alerts if threshold crossed
# Runs every 5min via systemd timer

import os, sys, json, time, math
from datetime import datetime, timedelta

import psycopg2
import urllib.request

DB_URL  = os.getenv('DATABASE_URL', 'postgresql://localhost:5432/atlas_db')
AI_URL  = os.getenv('AI_ROUTER_URL', 'http://127.0.0.1:3080')
GW_URL  = os.getenv('GATEWAY_URL', 'http://127.0.0.1:3147')
TG_TOKEN = os.getenv('TG_TOKEN', '')
TG_CHAT_ID = os.getenv('TG_CHAT_ID', '')

THRESHOLDS = {
    'ai_cost_usd_24h': 15.0,
    'gateway_requests_24h': 50000,
    'cache_miss_rate': 0.85,
    'error_rate_24h': 0.05,
}
PREDICTION_HOURS = 24

def fetch(url, timeout=5):
    try:
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f'[predictor] fetch error {url}: {e}')
        return None

def collect_metrics(conn):
    metrics = {}
    costs = fetch(f'{AI_URL}/costs')
    if costs:
        metrics['ai_cost_usd_24h'] = float(costs.get('total_usd', 0) or 0)
        metrics['ai_calls_24h'] = float(costs.get('calls', 0) or 0)
    cache = fetch(f'{AI_URL}/v1/cache/stats')
    if cache:
        total = (cache.get('cache_hits', 0) or 0) + (cache.get('cache_misses', 0) or 0)
        if total > 0:
            metrics['cache_miss_rate'] = (cache.get('cache_misses', 0) or 0) / total
    gw = fetch(f'{GW_URL}/v1/gateway/stats')
    if gw:
        metrics['gateway_requests_24h'] = float(gw.get('requests_24h', 0) or 0)
        errs = gw.get('errors_24h', 0) or 0
        reqs = max(gw.get('requests_24h', 1) or 1, 1)
        metrics['error_rate_24h'] = errs / reqs
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_stat_activity WHERE state = 'active'")
            metrics['pg_active_conns'] = float(cur.fetchone()[0])
    except Exception as e:
        print(f'[predictor] pg metric error: {e}')
    print(f'[predictor] collected {len(metrics)} metrics')
    return metrics

def save_metrics(conn, metrics):
    now = datetime.utcnow()
    with conn.cursor() as cur:
        for metric, value in metrics.items():
            cur.execute(
                'INSERT INTO predictive.metrics_history(metric, value, ts) VALUES(%s, %s, %s)',
                (metric, value, now)
            )
    conn.commit()

def linear_predict(values, hours_ahead):
    n = len(values)
    if n < 3:
        return None
    x = list(range(n))
    mx = sum(x) / n
    my = sum(values) / n
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, values))
    den = sum((xi - mx) ** 2 for xi in x)
    if den == 0:
        return values[-1]
    slope = num / den
    intercept = my - slope * mx
    return intercept + slope * (n - 1 + hours_ahead)

def predict_metrics(conn):
    predictions = {}
    with conn.cursor() as cur:
        cur.execute('''
            SELECT metric, array_agg(value ORDER BY ts) AS values
            FROM (
                SELECT metric, value, ts FROM predictive.metrics_history
                WHERE ts > NOW() - INTERVAL '48 hours'
                ORDER BY ts DESC
            ) sub GROUP BY metric
        ''')
        rows = cur.fetchall()
    for metric, values in rows:
        if not values:
            continue
        pred = linear_predict(list(reversed(values)), PREDICTION_HOURS)
        if pred is not None:
            predictions[metric] = pred
    print(f'[predictor] predictions: {predictions}')
    return predictions

def fire_alerts(conn, predictions):
    new_alerts = []
    for metric, threshold in THRESHOLDS.items():
        pred = predictions.get(metric)
        if pred is None:
            continue
        if pred > threshold:
            msg = f'Predicted {metric}={pred:.3f} (threshold:{threshold})'
            print(f'[predictor] ALERT: {msg}')
            with conn.cursor() as cur:
                cur.execute('''SELECT id FROM predictive.alerts
                    WHERE metric=%s AND created_at > NOW() - INTERVAL '1 hour' AND acked_at IS NULL''',
                    (metric,))
                if cur.fetchone():
                    continue
                cur.execute('''INSERT INTO predictive.alerts(metric,predicted_value,threshold,severity,message)
                    VALUES(%s,%s,%s,%s,%s)''', (metric, pred, threshold, 'warning', msg))
                conn.commit()
                new_alerts.append(msg)
    if new_alerts and TG_TOKEN and TG_CHAT_ID:
        text = json.dumps({'chat_id': TG_CHAT_ID, 'text': '🔮 Atlas Predictive Alert\n' + '\n'.join(new_alerts), 'parse_mode': 'Markdown'})
        try:
            req = urllib.request.Request(
                f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                data=text.encode(), headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            print(f'[predictor] TG error: {e}')

def main():
    print(f'[predictor] starting at {datetime.utcnow().isoformat()}')
    conn = psycopg2.connect(DB_URL)
    try:
        metrics = collect_metrics(conn)
        if metrics:
            save_metrics(conn, metrics)
            predictions = predict_metrics(conn)
            fire_alerts(conn, predictions)
        else:
            print('[predictor] no metrics collected')
    finally:
        conn.close()
    print('[predictor] done')

if __name__ == '__main__':
    main()
