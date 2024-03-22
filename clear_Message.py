from datetime import datetime
from data import Message
from context import UserMessageContext
def clear():
    global Message
    now = datetime.now()
    for UserObject in Message:  # 遍历 Message 中的用户对象
        delta_time = (now - UserObject.register_time).total_seconds()
        if delta_time > 3600:
            Message.remove(UserObject)