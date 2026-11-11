"""Aggregate node timings and GPU trace events without mixing profiled timings."""

import argparse
import gzip
import json
import pstats
import statistics
from collections import Counter
from pathlib import Path


def delta(row, name):
    return sum(v - row['before'][k] for k, v in row['after'].items()
               if k.startswith('vllm:' + name + '{'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    root = parser.parse_args().root
    summaries = []
    raw = {}
    for mode in ['off', 'on']:
        raw[mode] = [json.loads(x) for x in (root / f'nodes-{mode}.jsonl').read_text().splitlines()]
        for role in sorted({r['role'] for r in raw[mode]}):
            rows = [r for r in raw[mode] if r['role'] == role and r['index'] > 0]
            generated = sum(max(0, r['usage']['completion_tokens'] - 1) for r in rows)
            summaries.append({
                'mode': mode, 'role': role, 'n': len(rows),
                'ttft_ms': statistics.mean(delta(r, 'time_to_first_token_seconds_sum')
                                           for r in rows) * 1000,
                'prefill_ms': statistics.mean(delta(r, 'request_prefill_time_seconds_sum')
                                              for r in rows) * 1000,
                'elapsed_ms': statistics.mean(r['elapsed_s'] for r in rows) * 1000,
                'output_tokens_mean': statistics.mean(
                    r['usage']['completion_tokens'] for r in rows),
                'decode_per_token_ms': sum(delta(r, 'request_decode_time_seconds_sum')
                                           for r in rows) * 1000 / generated if generated else None,
                'cache_ratio': sum(delta(r, 'prefix_cache_hits_total') for r in rows)
                / sum(r['input_tokens'] for r in rows),
                'input_tokens_mean': statistics.mean(r['input_tokens'] for r in rows),
                'lcp_mean': statistics.mean(r['lcp'] for r in rows),
                'repeat_ttft_ms': [statistics.mean(delta(r, 'time_to_first_token_seconds_sum')
                                  for r in rows if r['repeat'] == rep) * 1000 for rep in range(3)],
            })
    def paired(rows):
        return {(r['role'], r['repeat'], r['index']): r for r in rows}
    off, on = paired(raw['off']), paired(raw['on'])
    checks = {'requests_per_mode': [len(off), len(on)],
              'same_tokens': all(off[k]['token_hash'] == on[k]['token_hash'] for k in off),
              'same_output_counts': sum(off[k]['usage']['completion_tokens'] ==
                                        on[k]['usage']['completion_tokens'] for k in off)}
    equal_length = []
    for role in sorted({k[0] for k in off}):
        pairs = [(off[k], on[k]) for k in off if k[0] == role and k[2] > 0
                 and off[k]['usage']['completion_tokens'] == on[k]['usage']['completion_tokens']]
        equal_length.append({'role': role, 'n': len(pairs),
                             'off_s': statistics.mean(a['elapsed_s'] for a, b in pairs),
                             'on_s': statistics.mean(b['elapsed_s'] for a, b in pairs)})
    traces = []
    for mode in ['off', 'on']:
        for item in json.loads((root / f'traces-{mode}.json').read_text()):
            for name in item['files']:
                # Keep both ranks' raw artifacts; compare rank 0 consistently,
                # never sum overlapping TP kernels as end-to-end wall time.
                if not (name.endswith('.json.gz') and Path(name).name.startswith('rank0.')):
                    continue
                with gzip.open(root / name, 'rt') as f:
                    data = json.load(f)
                kernels = [e for e in data.get('traceEvents', []) if e.get('cat') == 'kernel']
                top = Counter()
                for e in kernels:
                    top[e['name']] += e.get('dur', 0)
                traces.append({'mode': mode, 'role': item['role'], 'file': name,
                               'kernel_events': len(kernels),
                               'kernel_sum_ms': sum(top.values()) / 1000,
                               'top_kernels_us': top.most_common(8)})
    hosts = []
    for group in ['A', 'B']:
        stats = pstats.Stats(str(root / group / 'host.pstats'))
        case_dir = 'quality-reprofile' if group == 'A' else 'quality'
        functions = [(str(k), v[2], v[3]) for k, v in stats.stats.items()
                     if '/agentgpt/host/' in k[0]]
        hosts.append({'group': group, 'host_code_self_s': sum(x[1] for x in functions),
                      'top_host_self': sorted(functions, key=lambda x: x[1], reverse=True)[:15],
                      'process': json.loads((root / group / 'host-process.json').read_text()),
                      'case': json.loads(
                          (root / group / case_dir / 'multi_step-00.json').read_text())})
    (root / 'summary.json').write_text(json.dumps({'nodes': summaries, 'checks': checks,
                                                 'same_output_pairs': equal_length,
                                                 'traces': traces, 'hosts': hosts},
                                                ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'nodes': summaries, 'checks': checks}, indent=2))


if __name__ == '__main__':
    main()
