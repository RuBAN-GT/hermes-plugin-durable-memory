from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from hermes_durable_memory.config import Settings
from hermes_durable_memory.ollama import OllamaConfig, OllamaEmbeddingClient


class OllamaEmbeddingTests(unittest.TestCase):
    def test_disabled_configuration_fails_closed_without_network(self) -> None:
        client = OllamaEmbeddingClient(OllamaConfig())
        with patch("hermes_durable_memory.ollama.urlopen") as urlopen:
            self.assertIsNone(client.embed("hello"))
        urlopen.assert_not_called()

    def test_posts_only_to_embed_endpoint(self) -> None:
        response = MagicMock()
        response.read.return_value = json.dumps({"embeddings": [[1, 2.5]]}).encode()
        response.__enter__.return_value = response
        with patch(
            "hermes_durable_memory.ollama.urlopen", return_value=response
        ) as urlopen:
            vector = OllamaEmbeddingClient(
                OllamaConfig("http://localhost:11434", "nomic-embed-text")
            ).embed("hello")
        self.assertEqual(vector, [1.0, 2.5])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:11434/api/embed")
        self.assertEqual(
            json.loads(request.data),
            {"model": "nomic-embed-text", "input": "hello"},
        )
        self.assertEqual(request.method, "POST")

    def test_settings_enable_only_the_ollama_provider(self) -> None:
        settings = Settings.from_env(
            {
                "DURABLE_MEMORY_EMBEDDING_PROVIDER": "ollama",
                "DURABLE_MEMORY_OLLAMA_BASE_URL": "http://localhost:11434",
                "DURABLE_MEMORY_OLLAMA_MODEL": "model",
            }
        )
        self.assertTrue(OllamaEmbeddingClient.from_settings(settings).config.enabled)
        disabled = Settings.from_env({"DURABLE_MEMORY_EMBEDDING_PROVIDER": "other"})
        self.assertFalse(OllamaEmbeddingClient.from_settings(disabled).config.enabled)


if __name__ == "__main__":
    unittest.main()
