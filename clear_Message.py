from datetime import datetime
from data import Message
from context import UserMessageContext
def clear():    # 清理会话
    global Message
    now = datetime.now()
    for UserObject in Message:  # 遍历 Message 中的用户对象
        delta_time = (now - UserObject.register_time).total_seconds()   # 计算注册到现在的时间差
        if delta_time > 3600:   # 若时间差超过一小时则清理会话
            Message.remove(UserObject)