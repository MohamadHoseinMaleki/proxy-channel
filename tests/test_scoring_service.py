"""Unit tests for ScoringService orchestration. No PostgreSQL."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.identity import ProxySecret
from core.models import Proxy, utcnow
from modules.scoring.models import ScoreBreakdown, ScoreStatus
from modules.scoring.service import ScoringService

ScoringService.__test__ = False  # type: ignore[attr-defined]
ScoreBreakdown.__test__ = False  # type: ignore[attr-defined]


class TestScoringServiceUnit:
    def test_rejects_non_positive_batch_size(self) -> None:
        with pytest.raises(ValueError, match="batch_size"):
            ScoringService(MagicMock(), batch_size=0)

    @pytest.mark.asyncio
    async def test_run_batch_empty_when_nothing_claimed(self) -> None:
        mock_db = MagicMock()
        mock_session = AsyncMock()

        class DummyScope:
            async def __aenter__(self) -> AsyncMock:
                return mock_session

            async def __aexit__(self, *_args: object) -> None:
                return None

        mock_db.session_scope.return_value = DummyScope()
        service = ScoringService(mock_db)

        with patch.object(service, "claim_due_proxies", return_value=[]):
            results = await service.run_batch()
        assert results == []

    @pytest.mark.asyncio
    async def test_run_batch_does_not_touch_observations(self) -> None:
        mock_db = MagicMock()
        mock_session = AsyncMock()
        mock_session.add = MagicMock()

        class DummyScope:
            async def __aenter__(self) -> AsyncMock:
                return mock_session

            async def __aexit__(self, *_args: object) -> None:
                return None

        mock_db.session_scope.return_value = DummyScope()
        service = ScoringService(mock_db, batch_size=5)

        proxy = Proxy(
            id=7,
            server="1.1.1.1",
            port=443,
            secret=ProxySecret("dd" + "11" * 16),
            fingerprint="a" * 64,
            last_test_finished_at=utcnow(),
        )

        async def fake_claim(_session: object, *, limit: int) -> list[Proxy]:
            assert limit == 5
            return [proxy]

        with (
            patch.object(service, "claim_due_proxies", side_effect=fake_claim),
            patch.object(service, "_load_observations", return_value={7: []}),
        ):
            results = await service.run_batch()

        assert len(results) == 1
        assert results[0].proxy_id == 7
        assert results[0].status is ScoreStatus.NO_OBSERVATIONS_IN_WINDOW
        mock_session.add.assert_called_once()
        assert mock_session.delete.call_count == 0
        assert mock_session.execute.await_count == 0

    @pytest.mark.asyncio
    async def test_logs_do_not_include_secret_material(
        self, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        from core.logger import configure_logging
        from tests.conftest import make_settings

        configure_logging(make_settings(log_format="json", log_level="DEBUG"), force=True)
        mock_db = MagicMock()
        mock_session = AsyncMock()
        mock_session.add = MagicMock()

        class DummyScope:
            async def __aenter__(self) -> AsyncMock:
                return mock_session

            async def __aexit__(self, *_args: object) -> None:
                return None

        mock_db.session_scope.return_value = DummyScope()
        service = ScoringService(mock_db)
        secret = "dd" + "ab" * 16
        proxy = Proxy(
            id=3,
            server="198.51.100.9",
            port=443,
            secret=ProxySecret(secret),
            fingerprint="b" * 64,
            last_test_finished_at=utcnow() - timedelta(minutes=1),
        )

        with (
            patch.object(service, "claim_due_proxies", return_value=[proxy]),
            patch.object(service, "_load_observations", return_value={3: []}),
        ):
            await service.run_batch()

        output = json_logs.readouterr().out
        assert secret not in output
        assert "tg://" not in output
