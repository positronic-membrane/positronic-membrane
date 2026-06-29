def consolidate_memories():
    sdk['logger'].info("Auto-triggered background memory consolidation...")
    sdk['memory'].consolidate(batch_size=5)
    return "Memory consolidation executed successfully."
