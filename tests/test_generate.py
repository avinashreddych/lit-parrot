import functools
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock
from unittest.mock import Mock, call, ANY

import pytest
import torch

wd = Path(__file__).parent.parent.absolute()


@functools.lru_cache(maxsize=1)
def load_generate_script():
    sys.path.append(str(wd))

    import generate

    return generate


@pytest.fixture()
def fake_checkpoint_dir(tmp_path):
    os.chdir(tmp_path)
    checkpoint_dir = tmp_path / "checkpoints" / "tmp"
    checkpoint_dir.mkdir(parents=True)
    (checkpoint_dir / "lit_model.pth").touch()
    (checkpoint_dir / "lit_config.json").touch()
    (checkpoint_dir / "tokenizer.json").touch()
    (checkpoint_dir / "tokenizer_config.json").touch()
    return checkpoint_dir


@pytest.mark.parametrize("max_seq_length", (10, 9999))
def test_generate(max_seq_length):
    generate = load_generate_script()

    from lit_parrot import Parrot, Config

    T, C = 5, 3
    input_idx = torch.randint(10, size=(T,))

    config = Config(block_size=128, vocab_size=16, n_layer=1, n_head=4, n_embd=8)
    model = Parrot(config)
    max_new_tokens = 20

    multinomial_results = []
    original_multinomial = torch.multinomial

    def multinomial(*args, **kwargs):
        out = original_multinomial(*args, **kwargs)
        multinomial_results.append(out)
        return out

    with mock.patch("torch.multinomial", multinomial):
        out = generate.generate(model, input_idx, max_new_tokens, max_seq_length=max_seq_length, top_k=4)

    assert out.size(0) == min(T + max_new_tokens, max_seq_length)
    multinomial_results = torch.hstack(multinomial_results)
    expected = torch.cat((input_idx, multinomial_results))
    assert out.shape == expected.shape
    torch.testing.assert_close(out, expected)


@mock.patch("torch.cuda.is_bf16_supported", return_value=False)
def test_main(_, fake_checkpoint_dir, monkeypatch):
    generate = load_generate_script()

    config_path = fake_checkpoint_dir / "lit_config.json"
    config = {"block_size": 16, "vocab_size": 50, "n_layer": 2, "n_head": 4, "n_embd": 8, "rotary_percentage": 1}
    config_path.write_text(json.dumps(config))

    class FabricMock(Mock):
        @property
        def device(self):
            return torch.device("cpu")

    monkeypatch.setattr(generate.L, "Fabric", FabricMock)
    load_mock = Mock()
    load_mock.return_value = load_mock
    load_mock.__enter__ = Mock()
    load_mock.__exit__ = Mock()
    monkeypatch.setattr(generate, "lazy_load", load_mock)
    tokenizer_mock = Mock()
    tokenizer_mock.return_value.encode.return_value = torch.tensor([[1, 2, 3]])
    tokenizer_mock.return_value.decode.return_value = "foo bar baz"
    monkeypatch.setattr(generate, "Tokenizer", tokenizer_mock)
    generate_mock = Mock()
    generate_mock.return_value = torch.tensor([[3, 2, 1]])
    monkeypatch.setattr(generate, "generate", generate_mock)

    num_samples = 2
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        generate.main(temperature=2.0, top_k=2, num_samples=num_samples, checkpoint_dir=fake_checkpoint_dir)

    assert len(tokenizer_mock.return_value.decode.mock_calls) == num_samples
    assert torch.allclose(tokenizer_mock.return_value.decode.call_args[0][0], generate_mock.return_value)
    assert generate_mock.mock_calls == [call(ANY, ANY, 50, ANY, temperature=2.0, top_k=2)] * num_samples
    # only the generated result is printed to stdout
    assert out.getvalue() == "foo bar baz\n" * num_samples

    assert "'padded_vocab_size': 512, 'n_layer': 2, 'n_head': 4, 'n_embd': 8" in err.getvalue()


def test_cli():
    cli_path = wd / "generate.py"
    output = subprocess.check_output([sys.executable, cli_path, "-h"])
    output = str(output.decode())
    assert "Generates text samples" in output
