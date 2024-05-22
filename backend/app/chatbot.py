import openai
class Ai:
    def __init__(self,api_key):
        openai.api_key=api_key

    def get_response(self,message_list):
        response = openai.Completion.create(
            model="text-davinci-003",
            max_tokens=150
        )
        return response.choices[0].text.strip()
