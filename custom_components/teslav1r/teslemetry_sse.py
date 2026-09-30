"""Teslemetry Energy Site server-sent event client.

Teslemetry's Energy Site stream sends full ``live_status`` documents.  Keep
the transport deliberately small so PowerSync can use the new endpoint
without depending on an unreleased energy-site helper in teslemetry-stream.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import json
import logging
from typing import Any

import aiohttp


_LOGGER = logging.getLogger(__name__)

TESLEMETRY_LIVE_STATUS_TOPIC = "live_status"
TESLEMETRY_SSE_MAX_RETRY_SECONDS = 300

TokenGetter = Callable[[], Awaitable[str | None]]
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
ConnectionCallback = Callable[[bool], None]


class TeslemetrySSEHTTPError(Exception):
    """Raised when Teslemetry rejects an SSE connection."""

    def __init__(self, status: int) -> None:
        super().__init__(f"Teslemetry SSE returned HTTP {status}")
        self.status = status


async def iter_sse_json(content: Any) -> AsyncIterator[dict[str, Any]]:
    """Yield JSON objects from an aiohttp SSE response body.

    SSE permits comments/keep-alives and multi-line ``data:`` fields.  A
    malformed event is skipped without ending an otherwise healthy stream.
    """
    data_lines: list[str] = []

    async for raw_line in content:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = str(raw_line)
        line = line.rstrip("\r\n")

        if not line:
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines.clear()
                try:
                    event = json.loads(payload)
                except (TypeError, ValueError):
                    _LOGGER.warning("Ignoring malformed Teslemetry SSE JSON event")
                else:
                    if isinstance(event, dict):
                        yield event
            continue

        if line.startswith(":"):
            continue
        if line == "data":
            data_lines.append("")
        elif line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            data_lines.append(value)

    if data_lines:
        try:
            event = json.loads("\n".join(data_lines))
        except (TypeError, ValueError):
            _LOGGER.warning("Ignoring malformed final Teslemetry SSE JSON event")
        else:
            if isinstance(event, dict):
                yield event


class TeslemetryEnergySSEClient:
    """Maintain a reconnecting Teslemetry Energy Site SSE connection."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str,
        site_id: str,
        token_getter: TokenGetter,
        event_callback: EventCallback,
        connection_callback: ConnectionCallback,
        user_agent: str,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._site_id = str(site_id)
        self._token_getter = token_getter
        self._event_callback = event_callback
        self._connection_callback = connection_callback
        self._user_agent = user_agent

        self._active = False
        self._connected = False
        self._connection_generation = 0
        self._topics_supported = True
        self._response: aiohttp.ClientResponse | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        """Return whether the SSE HTTP response is currently open."""
        return self._connected

    def start(self, create_task: Callable[..., asyncio.Task[None]]) -> None:
        """Start the reconnect loop once."""
        if self._task and not self._task.done():
            return
        self._active = True
        coroutine = self._async_run()
        try:
            self._task = create_task(
                coroutine,
                name=f"power_sync_teslemetry_sse_{self._site_id}",
            )
        except TypeError:
            # Lightweight test/task factories and older HA builds may not
            # accept the optional task name.
            self._task = create_task(coroutine)

    async def async_stop(self) -> None:
        """Close the response and wait for the reconnect task to finish."""
        self._active = False
        if self._response is not None:
            self._response.close()
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._set_connected(False)

    def _set_connected(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        if connected:
            self._connection_generation += 1
        try:
            self._connection_callback(connected)
        except Exception:
            _LOGGER.debug(
                "Teslemetry SSE connection callback failed",
                exc_info=True,
            )

    async def _async_listen_once(self) -> None:
        token = await self._token_getter()
        if not token:
            raise RuntimeError("Teslemetry SSE token is temporarily unavailable")

        params = (
            {"topics": TESLEMETRY_LIVE_STATUS_TOPIC}
            if self._topics_supported
            else None
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
            "User-Agent": self._user_agent,
        }
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=15,
            sock_connect=15,
            sock_read=30,
        )
        url = f"{self._base_url}/sse/{self._site_id}"

        try:
            async with self._session.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout,
            ) as response:
                self._response = response
                if response.status != 200:
                    raise TeslemetrySSEHTTPError(response.status)

                self._set_connected(True)
                async for event in iter_sse_json(response.content):
                    if not self._active:
                        return
                    try:
                        await self._event_callback(event)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        _LOGGER.exception(
                            "Failed to process Teslemetry Energy Site SSE event"
                        )
        finally:
            self._response = None
            self._set_connected(False)

        if self._active:
            raise aiohttp.ServerDisconnectedError(
                "Teslemetry SSE stream ended"
            )

    async def _async_run(self) -> None:
        retries = 0
        try:
            while self._active:
                connection_generation = self._connection_generation
                try:
                    await self._async_listen_once()
                    retries = 0
                except asyncio.CancelledError:
                    raise
                except TeslemetrySSEHTTPError as err:
                    self._set_connected(False)
                    if err.status == 400 and self._topics_supported:
                        # A regional server may briefly lag the global rollout
                        # of the topics allowlist.  The site-specific legacy
                        # stream is still safe because PowerSync filters the
                        # payload to live_status locally.
                        self._topics_supported = False
                        _LOGGER.info(
                            "Teslemetry SSE topics filter unavailable; "
                            "retrying the site stream in compatibility mode"
                        )
                        continue
                    _LOGGER.warning(
                        "Teslemetry Energy Site SSE connection rejected "
                        "with HTTP %s; REST polling fallback remains active",
                        err.status,
                    )
                except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                    self._set_connected(False)
                    _LOGGER.debug(
                        "Teslemetry Energy Site SSE connection lost: %s",
                        err,
                    )
                except Exception as err:
                    self._set_connected(False)
                    _LOGGER.debug(
                        "Teslemetry Energy Site SSE unavailable: %s",
                        err,
                    )
                finally:
                    self._response = None
                    self._set_connected(False)

                if not self._active:
                    break
                if self._connection_generation != connection_generation:
                    # A connection that carried data for any length of time
                    # recovered successfully. Its next disconnect should get
                    # the fast one-second retry, not inherit an old outage's
                    # long backoff.
                    retries = 0
                delay = min(
                    2 ** min(retries, 8),
                    TESLEMETRY_SSE_MAX_RETRY_SECONDS,
                )
                retries += 1
                await asyncio.sleep(delay)
        finally:
            self._response = None
            self._set_connected(False)
