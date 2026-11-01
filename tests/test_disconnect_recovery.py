import asyncio
from contextlib import contextmanager
import gc
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec('telethon'), 'Protocol dependencies required')
class DisconnectRecoveryTests(unittest.IsolatedAsyncioTestCase):
    @contextmanager
    def offline_sender(self, *, fail_connect=False):
        from telethon.network.mtprotosender import MTProtoSender

        async def connect(sender):
            # Exercise the real sender's disconnect/error future, but replace
            # socket setup entirely. No real key, account or endpoint is used.
            sender._MTProtoSender__disconnected = asyncio.get_running_loop().create_future()
            if fail_connect:
                error = ConnectionResetError(22, 'synthetic connect reset', None, 64)
                sender._disconnected.set_exception(error)
                raise error

        with patch.object(MTProtoSender, '_connect', connect):
            yield

    def connection(self):
        from tatocollector.client import TatoConnection
        return TatoConnection({'layer': 123, 'api_id': 1, 'app_version': 'synthetic'},
                              Path('research/android-static/embedded-public-0.pem').read_bytes(),
                              auth_key=bytes(range(256)))

    async def capture_unretrieved(self, operation):
        loop = asyncio.get_running_loop()
        previous, reports = loop.get_exception_handler(), []
        loop.set_exception_handler(lambda loop, context: reports.append(context))
        try:
            await operation()
            for _ in range(3):
                await asyncio.sleep(0)
                gc.collect()
            return [report for report in reports if 'exception was never retrieved' in report.get('message', '')]
        finally:
            loop.set_exception_handler(previous)

    async def test_idle_reset_future_is_observed_after_sdk_has_already_disconnected(self):
        async def scenario():
            with self.offline_sender():
                async with self.connection() as connection:
                    await connection.sender._disconnect(error=ConnectionResetError(22, 'synthetic idle reset', None, 64))
                    await asyncio.sleep(0)

        self.assertEqual(await self.capture_unretrieved(scenario), [])

    async def test_reset_is_preserved_for_status_and_reconnect_instead_of_only_silenced(self):
        from tatocollector.client import safe_error
        with self.offline_sender():
            async with self.connection() as connection:
                error = ConnectionResetError(22, 'synthetic private error text', None, 64)
                await connection.sender._disconnect(error=error)
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                with self.assertRaises(ConnectionResetError) as caught:
                    connection.check_connected()
                self.assertIs(caught.exception, error)
                details = safe_error(caught.exception)
                self.assertEqual(details.get('winerror'), 64)
                self.assertNotIn('private error text', str(details))

    async def test_failed_connect_exception_future_is_also_retrieved_during_cleanup(self):
        async def scenario():
            with self.offline_sender(fail_connect=True):
                with self.assertRaises(ConnectionResetError):
                    async with self.connection():
                        self.fail('Synthetic failed connect must not enter the context')

        self.assertEqual(await self.capture_unretrieved(scenario), [])

    async def test_normal_close_and_cancelled_scope_do_not_create_unretrieved_futures(self):
        async def scenario():
            with self.offline_sender():
                async with self.connection() as connection:
                    connection.check_connected()
                with self.assertRaises(asyncio.CancelledError):
                    async with self.connection():
                        raise asyncio.CancelledError()

        self.assertEqual(await self.capture_unretrieved(scenario), [])


if __name__ == '__main__':
    unittest.main()
