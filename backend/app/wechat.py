from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from time import sleep


class WeChatBot:
    extension_path = 'wechat_plugins\\wechat_plugins.crx'

    def __init__(self, chatbot, storage, context):
        self.chatbot = chatbot
        self.storage = storage
        self.context = context
        self.chrome_options = Options()
        self.chrome_options.add_extension(self.extension_path)
        self.chrome_options.page_load_strategy = 'none'
        self.driver = webdriver.Chrome(service=Service('chromedriver-win64\\chromedriver.exe'),
                                       options=self.chrome_options)  # 使用Chrome浏览器
        self.driver.implicitly_wait(2)
        self.bot_name = " "

    def login(self):
        self.driver.get('https://wx.qq.com/')
        sleep(1)
        img_element = self.driver.find_element(By.CLASS_NAME, 'img')
        # 获取图片URL
        img_url = img_element.get_attribute('src')
        print(img_url)
        # 等待用户扫码登录
        sleep(20)
        name = self.driver.find_element(By.CLASS_NAME, "nickname").text
        print("WechatBotName=", name)
        return name

    def check_latest_message(self):
        message_lists = self.driver.find_elements(By.CSS_SELECTOR,
                                                  '[ng-repeat="chatContact in chatList track by chatContact.UserName"]')
        for message in message_lists:
            if len(message.find_element(By.CSS_SELECTOR, ".avatar").find_elements(By.CSS_SELECTOR, '*')) == 2:
                message.click()
                return 1
        return 0

    def get_latest_message(self):
        # 获取最新的微信消息
        username = (self.driver.find_elements(By.CSS_SELECTOR, '[ng-repeat="message in chatContent"]')[-1]
                    .find_element(By.CLASS_NAME, 'avatar').text)
        content = (self.driver.find_elements(By.CSS_SELECTOR, '[ng-repeat="message in chatContent"]')[-1]
                   .find_element(By.CLASS_NAME, 'plain').text)
        return [username, content]

    def send_message(self, message):
        # 发送消息
        input_box = self.driver.find_element(By.ID, 'editArea')
        input_box.send_keys(message)
        input_box.send_keys(Keys.RETURN)

    def run(self):
        self.bot_name = self.login()
        while True:
            if self.check_latest_message():
                user_message = self.get_latest_message()
                response = (self.chatbot.get_response(user_message[1]))
                self.send_message(response)
                self.storage.store_conversation(user_message, response)
            sleep(5)


if __name__ == '__main__':
    bot = WeChatBot(1, 1)
    bot.run()
