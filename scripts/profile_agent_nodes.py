"""Replay exact captured node requests; time without profiler, then capture traces."""

import argparse
import copy
import hashlib
import json
import secrets
import time
from collections import defaultdict
from pathlib import Path

import httpx


def role(body):
    system = body['messages'][0]['content']
    for marker, name in [('计划编排 agent', 'planner'), ('计划评审 agent', 'critic_plan'),
                         ('验收 agent', 'critic_verify'), ('成文 agent', 'synthesizer'),
                         ('分析本条用户原文', 'host_memory'),
                         ('将检索问题改写', 'retrieval_rewrite')]:
        if marker in system[:100]:
            return name
    if '执行 agent' in system[:100]:
        return 'worker_tools' if body.get('tools') else 'worker_writeup'
    return 'other'


def metrics(client, base):
    text = client.get(base + '/metrics').raise_for_status().text
    keep = ('time_to_first_token_seconds_sum', 'request_prefill_time_seconds_sum',
            'request_decode_time_seconds_sum', 'prefix_cache_hits_total',
            'prefix_cache_queries_total', 'num_requests_running')
    result = {}
    for line in text.splitlines():
        if any(line.startswith('vllm:' + k + '{') for k in keep):
            key, value = line.rsplit(' ', 1)
            result[key] = float(value)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--mode', choices=['on', 'off'], required=True)
    args = parser.parse_args()
    root, mode = args.root, args.mode
    groups = defaultdict(list)
    for line in (root / 'input.jsonl').read_text(encoding='utf-8').splitlines():
        row = json.loads(line)
        name = role(row['request'])
        # Keep the long initial real tool loop; other nodes use six real inputs.
        if len(groups[name]) < (20 if name == 'worker_tools' else 6):
            groups[name].append(row['request'])
    (root / 'selected.json').write_text(json.dumps(groups, ensure_ascii=False), encoding='utf-8')
    base = 'http://127.0.0.1:8010'
    output = root / ('nodes-' + mode + '.jsonl')
    with httpx.Client(timeout=180, trust_env=False) as client, output.open('w') as log:
        def generate(original, salt):
            body = copy.deepcopy(original)
            # LangChain captured max_completion_tokens; vLLM gives it priority
            # over max_tokens. Preserve the real role's full generation budget.
            if 'max_completion_tokens' in body:
                body.pop('max_tokens', None)
            body.update(temperature=0, seed=42, stream=False, cache_salt=salt)
            return body

        for repeat in range(3):
            for name, requests in groups.items():
                salt = secrets.token_urlsafe(32)
                previous = []
                for index, original in enumerate(requests):
                    body = generate(original, salt)
                    keys = ('model', 'messages', 'tools', 'chat_template_kwargs')
                    token_body = {k: body[k] for k in keys if k in body}
                    tokenized = client.post(base + '/tokenize', json=token_body)
                    tokens = tokenized.raise_for_status().json()['tokens']
                    lcp = next((i for i, pair in enumerate(zip(previous, tokens))
                                if pair[0] != pair[1]), min(len(previous), len(tokens)))
                    before = metrics(client, base)
                    start = time.perf_counter()
                    response = client.post(base + '/v1/chat/completions', json=body)
                    response.raise_for_status()
                    elapsed = time.perf_counter() - start
                    after = metrics(client, base)
                    record = {'mode': mode, 'role': name, 'repeat': repeat, 'index': index,
                              'elapsed_s': elapsed, 'usage': response.json().get('usage'),
                              'input_tokens': len(tokens), 'lcp': lcp,
                              'token_hash': hashlib.sha256(json.dumps(tokens).encode()).hexdigest(),
                              'before': before, 'after': after}
                    log.write(json.dumps(record) + '\n')
                    log.flush()
                    previous = tokens
                print('TIMED', mode, repeat, name, flush=True)

        # GPU traces are diagnostic only. Keep their overhead out of timings.
        traces = root / 'traces' / mode
        manifest = []
        for name, requests in groups.items():
            salt = secrets.token_urlsafe(32)
            for original in requests[:-1]:
                client.post(base + '/v1/chat/completions',
                            json=generate(original, salt)).raise_for_status()
            before_files = set(traces.rglob('*'))
            client.post(base + '/start_profile').raise_for_status()
            try:
                client.post(base + '/v1/chat/completions',
                            json=generate(requests[-1], salt)).raise_for_status()
            finally:
                client.post(base + '/stop_profile').raise_for_status()
            new_files = [str(p.relative_to(root)) for p in set(traces.rglob('*')) - before_files
                         if p.is_file()]
            manifest.append({'role': name, 'mode': mode, 'files': new_files})
            (root / ('traces-' + mode + '.json')).write_text(json.dumps(manifest, indent=2))
            print('PROFILED', mode, name, len(new_files), flush=True)


if __name__ == '__main__':
    main()
