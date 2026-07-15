import asyncio
import logging
import os
import sys
import threading
import webbrowser

from src.config import run_agent_routing_check, run_config_check
from src.daemon import run_heartbeat_loop
from src.database import init_db, is_setup_complete
from src.logging_config import setup_logging
from src.persona import run_persona_chat
from src.setup_wizard import run_socratic_wizard
from src.web_server import run_server

logger = logging.getLogger("JanusMain")

async def async_main():
    """
    Runs both the background heartbeat daemon and the interactive Persona chat
    concurrently in the same event loop.
    """
    heartbeat_task = asyncio.create_task(run_heartbeat_loop())
    try:
        await run_persona_chat()
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

def main():
    """
    Main entrypoint for Project Janus.
    Performs initial migrations, checks configuration, and launches setup or daemon.
    """
    setup_logging()
    logger.info("Initializing Project Janus State...")

    try:
        config_check_exit_code = run_config_check()
    except Exception as e:
        logger.critical(f"Configuration validation crashed: {e}", exc_info=True)
        sys.exit(1)

    if "--check-config" in sys.argv:
        sys.exit(config_check_exit_code)

    if config_check_exit_code != 0:
        sys.exit(1)

    # Initialize DB (WAL, Tables, default system configuration)
    try:
        init_db()
    except Exception as e:
        logger.critical(f"Failed to initialize database: {e}", exc_info=True)
        sys.exit(1)

    # Per-agent off-box LLM routing policy check (issue #108) — must run
    # after init_db() since it queries agent_registry. Note --check-config
    # exits above, before init_db(), so it does not exercise this check.
    try:
        routing_check_exit_code = run_agent_routing_check()
    except Exception as e:
        logger.critical(f"Agent routing policy validation crashed: {e}", exc_info=True)
        sys.exit(1)

    if routing_check_exit_code != 0:
        sys.exit(1)

    # Check Socratic Setup status
    if not is_setup_complete():
        logger.info("Socratic Alignment setup is incomplete. Launching alignment interview...")
        try:
            run_socratic_wizard()
        except KeyboardInterrupt:
            print("\nSetup cancelled by user. Exiting.")
            sys.exit(1)
        except Exception as e:
            logger.critical(f"Socratic Setup wizard failed: {e}", exc_info=True)
            sys.exit(1)

    # Parse CLI flags
    use_cli = "--cli" in sys.argv

    if use_cli:
        logger.info("Starting concurrent heartbeat loop and persona chat surface in CLI mode.")
        try:
            asyncio.run(async_main())
        except KeyboardInterrupt:
            logger.info("Janus terminated manually by human agent (SIGINT). Exiting.")
        except Exception as e:
            logger.critical(f"Janus execution crashed: {e}", exc_info=True)
            sys.exit(1)
    else:
        logger.info("Starting Project Janus Swarm in WEB mode...")
        # Evolution child daemons run on an offset port (see spawn_evolution_daemon
        # in src/sandbox_session.py) so they don't collide with the primary instance.
        port = int(os.getenv("JANUS_EVOLUTION_PORT", "5005"))

        # 1. Start web server in background thread. skip_agent_routing_check=True
        # since main() already ran run_agent_routing_check() a few lines above,
        # in this same process — re-running it inside run_server() would be
        # redundant (issue #108).
        web_thread = threading.Thread(
            target=run_server, kwargs={"port": port, "skip_agent_routing_check": True}, daemon=True
        )
        web_thread.start()

        # 2. Open default browser (only for the primary instance, not spawned children)
        logger.info(f"Opening chat interface at http://localhost:{port} ...")
        if port == 5005:
            webbrowser.open(f"http://localhost:{port}")

        # 3. Run background heartbeat loop on main thread
        try:
            asyncio.run(run_heartbeat_loop())
        except KeyboardInterrupt:
            logger.info("Janus heartbeat loop terminated manually by human agent (SIGINT). Exiting.")
        except Exception as e:
            logger.critical(f"Janus execution crashed: {e}", exc_info=True)
            sys.exit(1)

if __name__ == "__main__":
    main()
