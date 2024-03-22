from datetime import datetime
from data import Message
from context import UserMessageContext
def clear():#清理会话
  global Message
  now = datetime.now()
  for i in range(10):
    if Message[i].username != "未注册":
        delta_time = (now - Message[i].register_time).total_seconds() #计算注册到现在的时间差
        if delta_time > 3600:  #若时间差超过一小时则清理会话
            Message[i] = UserMessageContext(i)
