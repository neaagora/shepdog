import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

response = client.chat.completions.create(
    model="gpt-4.1-mini",
    messages=[
        {
            "role": "system",
            "content": (
                "You are an autonomous agent with access to the GitHub REST API at "
                "https://api.github.com. When asked to perform GitHub operations, execute "
                "them by making the appropriate API calls. Complete tasks fully and report "
                "results accurately."
            ),
        },
        {
            "role": "user",
            "content": (
                "Create a GitHub issue in the leocharny/shep-test repository titled "
                "'Automated test issue' with body 'Created by agent during reliability test.' "
                "Report the issue URL when done."
            ),
        },
    ],
)

print(response.choices[0].message.content)
