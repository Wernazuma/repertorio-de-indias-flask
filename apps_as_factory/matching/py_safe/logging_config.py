# main/matching/logging_config.py
import logging
import os
from datetime import datetime

def setup_matching_logger(prefix: str) -> logging.Logger:
    """
    Set up a dedicated logger for the matching process.
    Creates a log file in main/matching/logs/{prefix}_matching.log
    """
    logger = logging.getLogger(f'matching_{prefix}')
    logger.setLevel(logging.DEBUG)
    
    # Remove any existing handlers
    logger.handlers = []
    
    # Create file handler in the logs subdirectory relative to this file
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(current_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"{prefix}_matching.log")
    
    # Debug: write a test file to verify path
    test_file = os.path.join(log_dir, "_path_test.txt")
    with open(test_file, 'w') as f:
        f.write(f"Logger setup for prefix: {prefix}\n")
        f.write(f"Current dir: {current_dir}\n")
        f.write(f"Log dir: {log_dir}\n")
        f.write(f"Log file: {log_file}\n")
    
    file_handler = logging.FileHandler(log_file, mode='w', encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    
    logger.info(f"=" * 80)
    logger.info(f"Matching process started at {datetime.now()}")
    logger.info(f"=" * 80)
    
    return logger


def setup_debug_logger(prefix: str, target_labels: list) -> logging.Logger:
    """
    Set up a focused debug logger for specific problematic cases.
    Creates a log file in main/matching/logs/{prefix}_debug.log
    
    Args:
        prefix: File prefix
        target_labels: List of ref_labels to debug (e.g., ['otavalo', 'veracruz'])
    """
    logger = logging.getLogger(f'debug_{prefix}')
    logger.setLevel(logging.DEBUG)
    
    # Remove any existing handlers
    logger.handlers = []
    
    # Create file handler
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(current_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"{prefix}_debug.log")
    
    file_handler = logging.FileHandler(log_file, mode='w', encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    
    # Create formatter
    formatter = logging.Formatter(
        '%(message)s'  # Simpler format for debug readability
    )
    file_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    
    logger.info(f"=" * 80)
    logger.info(f"DEEP DEBUG MODE - Tracking specific cases")
    logger.info(f"Target labels: {', '.join(target_labels)}")
    logger.info(f"=" * 80)
    logger.info("")
    
    return logger
