"""Run with inspect-ai==0.3.266 in a separate venv; only the official mock model runs."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sys


def isolate_environment(root):
    clean = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH') if key in os.environ}
    for name in ('home', 'tmp', 'appdata'):
        (root / name).mkdir()
    clean.update(HOME=str(root / 'home'), USERPROFILE=str(root / 'home'),
        APPDATA=str(root / 'appdata'), LOCALAPPDATA=str(root / 'appdata'),
        TEMP=str(root / 'tmp'), TMP=str(root / 'tmp'), PYTHONUTF8='1', DOTENV_DISABLED='1')
    os.environ.clear()
    os.environ.update(clean)
    os.chdir(root)
    (root / '.env').write_text('', encoding='utf-8')


def generate_log(root):
    # Delayed imports keep module import inert and avoid a root-project dependency.
    from inspect_ai import Task, eval
    from inspect_ai.dataset import Sample
    from inspect_ai.model import ModelOutput, ModelUsage, get_model
    from inspect_ai.scorer import Score, mean, scorer
    from inspect_ai.solver import generate

    @scorer(metrics=[mean()])
    def deterministic_exact():
        async def score(state, target):
            return Score(value=float(state.output.completion == target.text), answer=state.output.completion)
        return score

    outputs = [ModelOutput.from_content(model='mockllm/model', content=answer) for answer in ('alpha', 'wrong')]
    for output in outputs:
        # These are deliberately synthetic counters, never billed/measured usage.
        output.usage = ModelUsage(input_tokens=0, output_tokens=0, total_tokens=0)
    model = get_model('mockllm/model', custom_outputs=outputs)
    task = Task(name='motte_native_mock_receipt',
        dataset=[Sample(id='native-1', input='Return alpha', target='alpha'),
                 Sample(id='native-2', input='Return beta', target='beta')],
        solver=generate(), scorer=deterministic_exact(),
        metadata={'provenance': 'actual-native-output', 'model_kind': 'official-mockllm', 'live_model': False})
    logs = eval(task, model=model, log_dir=str(root / 'logs'), log_format='json',
                log_samples=True, log_realtime=False, display='none', max_samples=1)
    if len(logs) != 1 or logs[0].status != 'success':
        raise RuntimeError(f'native mock evaluation failed: {[log.error for log in logs]}')
    return logs[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output_dir', type=Path, help='New scratch directory; must not already exist.')
    args = parser.parse_args()
    if not sys.flags.utf8_mode:
        parser.error('run Python with -X utf8 for portable native metadata decoding')
    version = importlib.metadata.version('inspect-ai')
    if version != '0.3.266':
        parser.error(f'inspect-ai==0.3.266 required, found {version}')
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    isolate_environment(root)
    external_attempts = []
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guarded(original):
        def connect(sock, address):
            if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1', 'localhost'):
                external_attempts.append(str(address))
                raise RuntimeError('external network disabled for native mock receipt')
            return original(sock, address)
        return connect

    socket.socket.connect, socket.socket.connect_ex = guarded(original_connect), guarded(original_connect_ex)
    try:
        log = generate_log(root)
    finally:
        socket.socket.connect, socket.socket.connect_ex = original_connect, original_connect_ex
    if external_attempts:
        raise RuntimeError('unexpected external connection attempt')
    source = Path(log.location)
    raw = source.read_bytes()
    receipt = {'package': 'inspect-ai', 'version': version, 'python': sys.version,
        'source': source.relative_to(root).as_posix(), 'source_bytes': len(raw),
        'source_sha256': hashlib.sha256(raw).hexdigest(), 'schema_version': log.version,
        'status': log.status, 'sample_count': len(log.samples), 'model': log.eval.model,
        'native_scores': [{key: score.value for key, score in sample.scores.items()} for sample in log.samples],
        'capture_kind': 'actual-native-output', 'model_kind': 'official-mockllm',
        'external_model_calls': 0, 'external_connection_attempts': 0, 'credentials_used': False,
        'live_model': False, 'reproduction_script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (root / 'generation-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
