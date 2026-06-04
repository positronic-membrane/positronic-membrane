import sys
import asyncio
import logging
import threading
import webbrowser
from src.database import init_db, is_setup_complete
from src.setup_wizard import run_socratic_wizard
from src.daemon import run_heartbeat_loop
from src.persona import run_persona_chat
from src.web_server import run_server

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
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
    logger.info("Initializing Project Janus State...")
    
    # Initialize DB (WAL, Tables, default system configuration)
    try:
        init_db()
    except Exception as e:
        logger.critical(f"Failed to initialize database: {e}", exc_info=True)
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
        # 1. Start web server in background thread
        web_thread = threading.Thread(target=run_server, kwargs={"port": 5005}, daemon=True)
        web_thread.start()

        # 2. Open default browser
        logger.info("Opening chat interface at http://localhost:5005 ...")
        webbrowser.open("http://localhost:5005")

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
