from datetime import datetime
from data import Message
from context import UserMessageContext
def clear():
  global Message
  now = datetime.now()
  for i in range(10):
    if Message[i].username != "未注册":
        delta_time = (now - Message[i].register_time).total_seconds()
        if delta_time > 3600:
            Message[i] = UserMessageContext(i)
