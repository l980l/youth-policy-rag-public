from youth_policy.hosting import get_service
from youth_policy.serverless import make_serverless_handler


handler = make_serverless_handler(get_service)
