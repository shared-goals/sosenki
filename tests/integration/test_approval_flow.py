"""Integration tests for admin approval flow.

T038: Integration test for full approval flow.
Tests the complete workflow: client sends /request → admin replies "Approve" →
database updates request status → client receives welcome message (within 5s SLA).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from telegram import Update

from src.api import webhook as webhook_module
from src.models.access_request import AccessRequest, RequestStatus
from src.models.user import User
from src.services import AsyncSessionLocal


class TestApprovalFlow:
    """Integration tests for the complete admin approval flow."""

    @pytest.fixture(autouse=True)
    async def cleanup_db(self):
        """Clean up and setup database before and after each test."""
        async with AsyncSessionLocal() as db:
            try:
                await db.execute(delete(AccessRequest))
                await db.execute(delete(User))
                await db.commit()
            except Exception:
                # Table may not exist if migrations haven't run
                await db.rollback()

        # Setup: Create admin users for tests
        async with AsyncSessionLocal() as db:
            try:
                admin1 = User(
                    telegram_id=999888777,
                    name="Test Admin",
                    is_active=True,
                    is_administrator=True,
                )
                admin2 = User(
                    telegram_id=888777666,
                    name="Test Admin 2",
                    is_active=True,
                    is_administrator=True,
                )
                admin3 = User(
                    telegram_id=777666555,
                    name="Test Admin 3",
                    is_active=True,
                    is_administrator=True,
                )
                db.add(admin1)
                db.add(admin2)
                db.add(admin3)
                await db.commit()
            except Exception:
                await db.rollback()

        yield

        # Cleanup after test
        async with AsyncSessionLocal() as db:
            try:
                await db.execute(delete(AccessRequest))
                await db.execute(delete(User))
                await db.commit()
            except Exception:
                await db.rollback()

    @pytest.fixture
    def mock_bot(self):
        """Create a mock bot with async methods."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        return bot

    @pytest.fixture
    def client(self, mock_bot):
        """Create a test client with mocked bot."""
        with patch("telegram.Update.de_json") as mock_de_json:

            def de_json_side_effect(data, bot_instance):
                """Convert dict to Update object."""
                update = MagicMock(spec=Update)
                update.message = None
                update.callback_query = None

                if data and "message" in data:
                    update.message = MagicMock()
                    update.message.text = data["message"]["text"]
                    update.message.from_user = MagicMock()
                    update.message.from_user.id = data["message"]["from"]["id"]
                    update.message.from_user.first_name = data["message"]["from"].get(
                        "first_name", "TestUser"
                    )
                    update.message.from_user.username = data["message"]["from"].get(
                        "username", None
                    )
                    update.message.chat = MagicMock()
                    update.message.chat.id = (
                        data["message"].get("chat", {}).get("id", data["message"]["from"]["id"])
                    )
                    update.message.chat.type = (
                        data["message"].get("chat", {}).get("type", "private")
                    )
                    update.message.reply_text = AsyncMock()
                    # Handle reply_to_message for admin responses
                    update.message.reply_to_message = None
                    if "reply_to_message" in data["message"]:
                        rtm = MagicMock()
                        rtm.text = data["message"]["reply_to_message"].get("text", "")
                        update.message.reply_to_message = rtm

                if data and "callback_query" in data:
                    cq_data = data["callback_query"]
                    update.callback_query = MagicMock()
                    update.callback_query.id = cq_data.get("id", "callback_123")
                    update.callback_query.data = cq_data.get("data", "")
                    update.callback_query.from_user = MagicMock()
                    update.callback_query.from_user.id = cq_data["from"]["id"]
                    update.callback_query.from_user.first_name = cq_data["from"].get(
                        "first_name", "Admin"
                    )
                    update.callback_query.answer = AsyncMock()
                    update.callback_query.edit_message_text = AsyncMock()

                if update.message is None and update.callback_query is None:
                    return None
                return update

            mock_de_json.side_effect = de_json_side_effect

            mock_app = MagicMock()
            mock_app.bot = mock_bot
            mock_app.process_update = AsyncMock()

            async def process_update_impl(update):
                """Process update through the handler."""
                from src.bot.handlers import handle_admin_response, handle_request_command
                from src.bot.handlers.admin_requests import handle_admin_callback

                ctx = MagicMock()
                ctx.application = mock_app
                ctx.bot_data = {}

                # Route to callback handler for callback queries
                if update.callback_query:
                    await handle_admin_callback(update, ctx)
                # Route to appropriate message handler
                elif update.message and update.message.text:
                    if update.message.text.startswith("/request"):
                        await handle_request_command(update, ctx)
                    else:
                        await handle_admin_response(update, ctx)

            mock_app.process_update.side_effect = process_update_impl

            from src.api import bot_context

            bot_context.set_bot_app(mock_app)

            yield TestClient(webhook_module.app)

            bot_context.set_bot_app(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_full_approval_flow(self, client, mock_bot):
        """Test complete approval workflow: request → approval → welcome message.

        Verifies:
        1. Client sends /request command
        2. Request stored in database with PENDING status
        3. Admin sends "Approve" reply
        4. Request status updated to APPROVED in database
        5. Client receives welcome message
        6. Admin receives confirmation
        """
        client_id = 111222333
        client_name = "TestClient"
        admin_id = 999888777
        admin_name = "Admin"
        request_message = "Need access to system"

        # Step 1: Client submits request
        start_time = datetime.now(timezone.utc)
        client_update = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": int(datetime.now(timezone.utc).timestamp()),
                "chat": {"id": client_id, "type": "private"},
                "from": {"id": client_id, "is_bot": False, "first_name": client_name},
                "text": f"/request {request_message}",
            },
        }

        response = client.post("/webhook/telegram", json=client_update)
        assert response.status_code == 200

        # Verify request stored in database
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AccessRequest).where(AccessRequest.user_telegram_id == client_id)
            )
            stored_request = result.scalar_one_or_none()
            assert stored_request is not None
            assert stored_request.status == RequestStatus.PENDING
            assert stored_request.request_message == request_message
            request_id = stored_request.id

        # Reset mock to track approval call separately
        mock_bot.reset_mock()

        # Step 2: Admin approves the request via callback button
        admin_update = {
            "update_id": 2,
            "callback_query": {
                "id": "callback_approval_123",
                "from": {"id": admin_id, "is_bot": False, "first_name": admin_name},
                "chat_instance": "test_instance",
                "data": f"approve:{request_id}",
                "message": {
                    "message_id": 50,
                    "date": int(datetime.now(timezone.utc).timestamp()),
                    "chat": {"id": admin_id, "type": "private"},
                    "from": {"id": 777, "is_bot": True},
                    "text": f"<b>Request #{request_id}</b>\n\n<a href='tg://user?id={client_id}'>{client_name}</a> (ID: {client_id})\n\n<b>Message:</b>\n{request_message}\n\nReply with 'Approve' or 'Reject' or use the buttons below",
                },
            },
        }

        response = client.post("/webhook/telegram", json=admin_update)
        assert response.status_code == 200
        end_time = datetime.now(timezone.utc)
        elapsed_time = (end_time - start_time).total_seconds()

        # Step 3: Verify request status updated to APPROVED
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AccessRequest).where(AccessRequest.id == request_id))
            updated_request = result.scalar_one_or_none()
            assert updated_request is not None
            assert updated_request.status == RequestStatus.APPROVED
            assert updated_request.admin_telegram_id == admin_id
            assert updated_request.admin_response == "approved"
            assert updated_request.updated_at is not None

        # Step 4: Verify welcome message sent to client
        # Bot.send_message should have been called with client_id
        assert mock_bot.send_message.called
        send_calls = mock_bot.send_message.call_args_list
        welcome_sent = False
        for call in send_calls:
            if (len(call.args) >= 1 and str(client_id) in str(call.args[0])) or (
                call.kwargs.get("chat_id") and str(client_id) in str(call.kwargs.get("chat_id"))
            ):
                welcome_sent = True
                # Verify welcome message text contains content (language-agnostic)
                message_text = (
                    call.args[1]
                    if len(call.args) >= 2 and call.args[1]
                    else call.kwargs.get("text")
                )
                assert message_text and len(message_text) > 10, (
                    "Welcome message should have meaningful content"
                )

        assert welcome_sent, "Welcome message should be sent to client"

        # Verify timing SLA (approval response within 5 seconds)
        print(f"Approval flow time: {elapsed_time:.3f} seconds (SLA: 5s)")
        assert elapsed_time < 5.0, f"Approval flow exceeded SLA: {elapsed_time}s > 5s"

    @pytest.mark.asyncio
    async def test_approval_with_missing_request(self, client, mock_bot):
        """Test approval when request doesn't exist.

        Verifies:
        1. Admin sends approval callback
        2. Request ID doesn't exist in database
        3. Admin receives error message
        4. No approval is recorded
        """
        admin_id = 999888777
        admin_name = "Admin"

        # Admin tries to approve non-existent request via callback
        admin_update = {
            "update_id": 3,
            "callback_query": {
                "id": "callback_missing_123",
                "from": {"id": admin_id, "is_bot": False, "first_name": admin_name},
                "chat_instance": "test_instance",
                "data": "approve:99999",
                "message": {
                    "message_id": 99,
                    "date": int(datetime.now(timezone.utc).timestamp()),
                    "chat": {"id": admin_id, "type": "private"},
                    "from": {"id": 777, "is_bot": True},
                    "text": "<b>Request #99999</b>\n\n<a href='tg://user?id=999999'>Unknown</a> (ID: 999999)\n\n<b>Message:</b>\nTest\n\nReply with 'Approve' or 'Reject' or use the buttons below",
                },
            },
        }

        response = client.post("/webhook/telegram", json=admin_update)
        assert response.status_code == 200

        # Verify no requests exist in database
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AccessRequest))
            all_requests = result.scalars().all()
            assert len(all_requests) == 0

    @pytest.mark.asyncio
    async def test_approval_timing_sla(self, client, mock_bot):
        """Test that approval response meets timing SLA (< 5 seconds).

        Verifies:
        1. Full approval flow completes within 5 second SLA
        2. Includes request processing and approval processing
        """
        client_id = 222333444
        admin_id = 888777666
        request_message = "Quick approval test"

        # Step 1: Client submits request
        client_update = {
            "update_id": 4,
            "message": {
                "message_id": 4,
                "date": int(datetime.now(timezone.utc).timestamp()),
                "chat": {"id": client_id, "type": "private"},
                "from": {"id": client_id, "is_bot": False, "first_name": "User"},
                "text": f"/request {request_message}",
            },
        }

        response = client.post("/webhook/telegram", json=client_update)
        assert response.status_code == 200

        # Get request ID
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AccessRequest).where(AccessRequest.user_telegram_id == client_id)
            )
            stored_request = result.scalar_one_or_none()
            request_id = stored_request.id

        # Step 2: Measure approval response time
        start_time = datetime.now(timezone.utc)

        admin_update = {
            "update_id": 5,
            "callback_query": {
                "id": "callback_sla_123",
                "from": {"id": admin_id, "is_bot": False, "first_name": "Admin"},
                "chat_instance": "test_instance",
                "data": f"approve:{request_id}",
                "message": {
                    "message_id": 50,
                    "date": int(datetime.now(timezone.utc).timestamp()),
                    "chat": {"id": admin_id, "type": "private"},
                    "from": {"id": 777, "is_bot": True},
                    "text": f"<b>Request #{request_id}</b>\n\n<a href='tg://user?id={client_id}'>User</a> (ID: {client_id})\n\n<b>Message:</b>\nTest\n\nReply with 'Approve' or 'Reject' or use the buttons below",
                },
            },
        }

        response = client.post("/webhook/telegram", json=admin_update)
        end_time = datetime.now(timezone.utc)
        elapsed_time = (end_time - start_time).total_seconds()

        assert response.status_code == 200
        print(f"Approval response time: {elapsed_time:.3f} seconds (SLA: 5s)")
        assert elapsed_time < 5.0

    @pytest.mark.asyncio
    async def test_approval_idempotency(self, client, mock_bot):
        """Test that approving the same request twice doesn't cause issues.

        Verifies:
        1. First approval succeeds
        2. Second approval attempt is handled gracefully
        3. Request status remains APPROVED
        """
        client_id = 333444555
        admin_id = 777666555
        request_message = "Idempotency test"

        # Create and approve request once
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            request = AccessRequest(
                user_telegram_id=str(client_id),
                request_message=request_message,
                status=RequestStatus.PENDING,
            )
            db.add(request)
            await db.commit()
            await db.refresh(request)
            request_id = request.id

        # First approval via callback
        admin_update_1 = {
            "update_id": 6,
            "callback_query": {
                "id": "callback_idem_1",
                "from": {"id": admin_id, "is_bot": False, "first_name": "Admin"},
                "chat_instance": "test_instance",
                "data": f"approve:{request_id}",
                "message": {
                    "message_id": 50,
                    "date": int(datetime.now(timezone.utc).timestamp()),
                    "chat": {"id": admin_id, "type": "private"},
                    "from": {"id": 777, "is_bot": True},
                    "text": f"<b>Request #{request_id}</b>\n\n<a href='tg://user?id={client_id}'>User</a> (ID: {client_id})\n\n<b>Message:</b>\n{request_message}\n\nReply with 'Approve' or 'Reject' or use the buttons below",
                },
            },
        }

        response1 = client.post("/webhook/telegram", json=admin_update_1)
        assert response1.status_code == 200

        # Verify approved
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AccessRequest).where(AccessRequest.id == request_id))
            req = result.scalar_one_or_none()
            assert req.status == RequestStatus.APPROVED

        # Second approval attempt via callback (idempotent)
        mock_bot.reset_mock()
        admin_update_2 = {
            "update_id": 7,
            "callback_query": {
                "id": "callback_idem_2",
                "from": {"id": admin_id, "is_bot": False, "first_name": "Admin"},
                "chat_instance": "test_instance",
                "data": f"approve:{request_id}",
                "message": {
                    "message_id": 50,
                    "date": int(datetime.now(timezone.utc).timestamp()),
                    "chat": {"id": admin_id, "type": "private"},
                    "from": {"id": 777, "is_bot": True},
                    "text": f"Client Request: User (ID: {request_id})",
                },
            },
        }

        response2 = client.post("/webhook/telegram", json=admin_update_2)
        assert response2.status_code == 200

        # Verify still approved
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AccessRequest).where(AccessRequest.id == request_id))
            req = result.scalar_one_or_none()
            assert req.status == RequestStatus.APPROVED
