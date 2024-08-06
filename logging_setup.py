import logging

def configure_logging(app):
    log_file = app.config['LOG_FILE']
    log_level = logging.INFO

    logger = logging.getLogger()
    logger.setLevel(log_level)

    file_handler = logging.FileHandler(log_file)
    console_handler = logging.StreamHandler()

    file_handler.setLevel(log_level)
    console_handler.setLevel(log_level)

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    class IgnoreHTTPRequestFilter(logging.Filter):
        def filter(self, record):
            return not ('GET /' in record.getMessage() or 'POST /' in record.getMessage())

    file_handler.addFilter(IgnoreHTTPRequestFilter())
    console_handler.addFilter(IgnoreHTTPRequestFilter())

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # Custom logger for SQL queries
    query_logger = logging.getLogger('query_logger')
    query_logger.setLevel(logging.INFO)
    query_file_handler = logging.FileHandler('query_log.txt')
    query_file_handler.setLevel(logging.INFO)
    query_file_handler.setFormatter(formatter)
    query_logger.addHandler(query_file_handler)

    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
