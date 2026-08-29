from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC
from pathlib import Path
from urllib.parse import quote_plus

import discord
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from docket.domain.errors import DocketError
from docket.services.ingress_ledger import IngressIdentity, IngressLedgerService

logger = logging.getLogger("docket.discord_ingress")
_SEMANTIC_OPTION_PREFIX = "dkt:s:"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _read_secret(name: str) -> str:
    value = Path(_required(name)).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"{name} is empty")
    return value


def ingress_database_url() -> str:
    password = quote_plus(_read_secret("DOCKET_INGRESS_DB_PASSWORD_FILE"))
    host = os.environ.get("DOCKET_INGRESS_DB_HOST", "postgres")
    port = os.environ.get("DOCKET_INGRESS_DB_PORT", "5432")
    database = os.environ.get("DOCKET_INGRESS_DB_NAME", "docket")
    return f"postgresql+psycopg://docket_ingress:{password}@{host}:{port}/{database}"


class StableDiscordIngress(discord.Client):
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.session_factory = session_factory
        self.identity = IngressIdentity(
            operator_id=_required("DOCKET_OPERATOR_DISCORD_USER_ID"),
            guild_id=_required("DOCKET_DISCORD_GUILD_ID"),
            chat_channel_id=_required("DOCKET_CHAT_CHANNEL_ID"),
            queue_channel_id=_required("DOCKET_QUEUE_CHANNEL_ID"),
        )
        self.signing_key = _read_secret("DOCKET_INTERACTION_SIGNING_KEY_FILE").encode()

    def _capture_message(self, message: discord.Message) -> dict[str, object]:
        parent_id = getattr(message.channel, "parent_id", None)
        reply_to = message.reference.message_id if message.reference is not None else None
        with self.session_factory.begin() as session:
            return IngressLedgerService(
                session,
                identity=self.identity,
                signing_key=self.signing_key,
            ).capture_message(
                actor_id=str(message.author.id),
                guild_id=str(message.guild.id) if message.guild is not None else "",
                channel_id=str(message.channel.id),
                parent_channel_id=str(parent_id) if parent_id is not None else None,
                message_id=str(message.id),
                reply_to_message_id=str(reply_to) if reply_to is not None else None,
                verbatim_text=message.content,
                said_at=message.created_at.astimezone(UTC),
            )

    def _capture_selection(
        self, interaction: discord.Interaction[discord.Client], token: str
    ) -> dict[str, object]:
        message = interaction.message
        if message is None:
            raise DocketError(
                code="semantic_option_binding_mismatch",
                message="The selected option has no source projection.",
            )
        parent_id = getattr(interaction.channel, "parent_id", None)
        with self.session_factory.begin() as session:
            return IngressLedgerService(
                session,
                identity=self.identity,
                signing_key=self.signing_key,
            ).capture_semantic_selection(
                actor_id=str(interaction.user.id),
                guild_id=str(interaction.guild_id or ""),
                channel_id=str(interaction.channel_id or ""),
                parent_channel_id=str(parent_id) if parent_id is not None else None,
                interaction_id=str(interaction.id),
                message_id=str(message.id),
                option_token=token,
                responded_at=interaction.created_at.astimezone(UTC),
            )

    async def on_ready(self) -> None:
        Path("/tmp/docket-ingress-ready").touch()
        logger.info("deployment-stable Discord ingress connected as %s", self.user)

    async def on_disconnect(self) -> None:
        Path("/tmp/docket-ingress-ready").unlink(missing_ok=True)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or str(message.author.id) != self.identity.operator_id:
            return
        try:
            await asyncio.to_thread(self._capture_message, message)
        except DocketError as exc:
            if exc.code != "unauthorized_ingress":
                logger.exception("Discord message ledger capture rejected: %s", exc.code)
        except Exception:
            logger.exception("Discord message ledger capture failed")

    async def on_interaction(self, interaction: discord.Interaction[discord.Client]) -> None:
        data = interaction.data
        custom_id = data.get("custom_id", "") if isinstance(data, dict) else ""
        if not isinstance(custom_id, str) or not custom_id.startswith(_SEMANTIC_OPTION_PREFIX):
            return
        try:
            result = await asyncio.to_thread(
                self._capture_selection,
                interaction,
                custom_id.removeprefix(_SEMANTIC_OPTION_PREFIX),
            )
        except DocketError as exc:
            logger.warning("Discord semantic selection rejected: %s", exc.code)
            await interaction.response.send_message(
                f"Docket could not record that selection (`{exc.code}`).",
                ephemeral=True,
            )
            return
        except Exception:
            logger.exception("Discord semantic selection ledger capture failed")
            await interaction.response.send_message(
                "Docket could not durably record that selection. Nothing was authorized.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Decision recorded and queued. No further authorization is needed.",
            ephemeral=True,
        )
        logger.info(
            "stored semantic selection %s as %s",
            result["deferred_ingress_ref"],
            result["utterance_ref"],
        )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("DOCKET_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    engine = create_engine(ingress_database_url(), pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    StableDiscordIngress(factory).run(_read_secret("DOCKET_DISCORD_BOT_TOKEN_FILE"))
