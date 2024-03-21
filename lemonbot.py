import main
from journal_append import journal_append
from UIfunction import WxSend
from data import Wx, Message, password
global Wx, Message, password
# 日志记录启动时间
journal_append("bot日志.txt", "   Lemon bot启动\n")
# main程序启动并自动重启
while main.main():
    Wx.ButtonControl(Name="易安").Click()
    WxSend("易安", "尝试重启")
    journal_append("bot日志.txt", "   Lemon bot尝试自动重启\n")
