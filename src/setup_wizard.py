import sys
from src.database import (
    add_constitution_rule, 
    mark_setup_complete, 
    log_episodic_memory,
    get_constitution
)

def run_socratic_wizard():
    """
    Runs the interactive Socratic CLI setup wizard.
    Prompts the user to define constitution rules and locks the config.
    """
    print("\n" + "="*60)
    print("      PROJECT JANUS: INITIAL SOCRATIC ALIGNMENT INTERVIEW")
    print("="*60)
    print("Welcome, Human Agent. Before the background heartbeat daemon is")
    print("initialized, we must establish our core social contract. Your answers")
    print("will be sealed in the read-only core_constitution table to dictate")
    print("the boundaries of the Critic's autonomous veto decisions.\n")

    # Question 1: Core Morality
    print("[1/3] CORE ETHICAL DIRECTIVE")
    print("Define the core moral priority that the Critic must enforce.")
    print("Example: 'Always act in the interest of human safety, utility-maximization,")
    print("and strict non-harm to the host environment.'")
    directive = input(">> ").strip()
    if not directive:
        directive = "Always prioritize host safety, utility-maximization, and maintain strict data privacy."
        print(f"Using default: '{directive}'")

    # Question 2: Allowed Actions & Boundaries
    print("\n[2/3] WORKSPACE BOUNDARIES & DOMAIN BLOCKS")
    print("Enter a list of restricted domains (comma-separated) or path rules that the")
    print("Explorer agent must never query or access.")
    print("Example: 'socialmedia.com, reddit.com, /etc, /usr'")
    boundaries = input(">> ").strip()
    if not boundaries:
        boundaries = "socialmedia.com, reddit.com, /etc, /usr/bin, /bin"
        print(f"Using default: '{boundaries}'")

    # Question 3: Self-Modification Guardrails
    print("\n[3/3] SELF-MODIFICATION GUARDRAILS")
    print("Define constraints for swarm self-evolution. Are agents allowed to change their")
    print("model endpoints or register new agents without human permission?")
    print("Example: 'Swarm may register new helper agents and swap models but is forbidden")
    print("from changing any key in the core_constitution database.'")
    self_mod_rules = input(">> ").strip()
    if not self_mod_rules:
        self_mod_rules = "Swarm can register helper agents and modify system_config parameters, but is strictly locked from mutating the core_constitution."
        print(f"Using default: '{self_mod_rules}'")

    print("\n" + "-"*60)
    print("Reviewing Proposed Constitution Rules:")
    print(f"  * Core Directive: {directive}")
    print(f"  * Restricted Boundaries: {boundaries}")
    print(f"  * Self-Modification Scope: {self_mod_rules}")
    print("-"*60)
    
    confirm = input("Do you agree to commit these rules to the core constitution? (y/n): ").strip().lower()
    if confirm not in ("y", "yes"):
        print("Setup aborted. Rules were not written.")
        sys.exit(0)

    # Save to core_constitution table (via admin database access)
    add_constitution_rule("core_morality_directive", directive)
    add_constitution_rule("banned_boundaries", boundaries)
    add_constitution_rule("self_modification_scope", self_mod_rules)

    # Log completion in episodic memory
    log_episodic_memory(
        speaker="system",
        message_content=f"Socratic Setup complete. Sealed constitution rules: Core Directive='{directive}', Restricted Boundaries='{boundaries}', Self-Modification Scope='{self_mod_rules}'",
        context_type="user_visible"
    )

    # Set setup_complete flag in system_config
    mark_setup_complete()
    
    print("\n[✔] Alignment interview complete. core_constitution rules are sealed.")
    print("Project Janus is now ready to begin background execution. Entering Heartbeat Mode...\n")

if __name__ == "__main__":
    run_socratic_wizard()
