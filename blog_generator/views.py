
from django.contrib.auth.models import User
from django.contrib.auth import authenticate, login, logout
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
import json
from pytubefix import YouTube
from django.conf import settings
import os
import assemblyai as aai
import requests
from .models import BlogPost
from dotenv import load_dotenv

load_dotenv()
# Create your views here.
@login_required
def index(request):
     return render(request, 'index.html')



@csrf_exempt
def generate_blog(request):
     if request.method == 'POST':
          try:
               data = json.loads(request.body)
               yt_link = data['link']
          except (KeyError, json.JSONDecodeError):
               return JsonResponse({'error': 'Invalid input data'}, status=400)
              
          title = yt_title(yt_link)
          
          transcription = get_transcription(yt_link)
          if not transcription:
               return JsonResponse({'error': 'Transcription failed'}, status=500)
          
          blog_content = generate_blog_from_transcription(transcription)
          if not blog_content:
               return JsonResponse({'error': 'Blog generation failed'}, status=500)
          
          new_blog_article = BlogPost.objects.create(
               user = request.user,
               youtube_title = title,
               youtube_link = yt_link,
               generated_content = blog_content
          )
          new_blog_article.save()
          return JsonResponse({'content': blog_content}, status=200)
     else:
          return JsonResponse({'error': 'invalid request method'}, status=405)



def yt_title(link):
     yt = YouTube(link)
     title = yt.title
     return title

def download_audio(link):
     yt = YouTube(link)
     audio_stream = yt.streams.filter(only_audio=True).first()
     out_file = audio_stream.download(output_path=settings.MEDIA_ROOT)
     base, ext = os.path.splitext(out_file)
     new_file = base + '.mp3'
     os.rename(out_file, new_file)
     return new_file

def get_transcription(link):
     audio_file = download_audio(link)
     aai.settings.api_key = "95033509eb1b4ad795a16a8fbd759112"
     transcriber = aai.Transcriber()
     transcript = transcriber.transcribe(audio_file)
     
     return transcript.text



def generate_blog_from_transcription(transcription):
     
     prompt = (
     "Based on the following transcript from a YouTube video, write a comprehensive blog article. "
     "Write it based on the transcript, but don't make it look like a YouTube video. "
     "Make it look like a properly formatted blog article with paragraph spacing and clean headings. "
     "Seriously avoid using asterisks or markdown symbols like '**' or '*'. Use normal title casing for headings.\n\n"
     f"{transcription}\n\nBlog article:"
     )

     
     response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}",  # Get one (free): https://console.groq.com
            "Content-Type": "application/json",
        },
        json={
            "model": "llama3-70b-8192",  # Or llama3-8b-8192 (lighter)
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
            "max_tokens": 1000
        }
     )
     
     result = response.json()
     return result["choices"][0]["message"]["content"].strip()

     
def blog_list(request):
     blog_articles = BlogPost.objects.filter(user=request.user)
     return render(request, 'all-blogs.html', {'blog_articles': blog_articles})

def blog_details(request, pk):
    blog_article_detail = BlogPost.objects.get(id=pk)
    if request.user == blog_article_detail.user:
            return render(request, 'blog-details.html', {'blog_article_detail': blog_article_detail})
    else:
         return redirect('/')

#Authentication
def user_login(request):
     if request.method == 'POST':
          username = request.POST['username']
          password = request.POST['password']
          
          user = authenticate(request, username=username, password=password)
          if user is not None:
               login(request, user)
               return redirect('/')
          else:
               error_message = "Invalid username or password."
               return render(request, 'login.html', {'error_message': error_message})
     return render(request, 'login.html')

def user_signup(request):
     if request.method == 'POST':
          username = request.POST['username']
          email =  request.POST['email']
          password = request.POST['password']
          repeatPassword = request.POST['repeatPassword']
          
          if password == repeatPassword:
             try:
                  user = User.objects.create_user(username, email, password) 
                  user.save()
                  login(request, user) 
                  return redirect('/')
             except:
                  error_message = "User registration failed."
                  return render(request, 'signup.html', {'error_message': error_message})
          else:
               error_message = "Passwords do not match."
               return render(request, 'signup.html', {'error_message': error_message})
     return render(request, 'signup.html')

def user_logout(request):
     logout(request)
     return redirect('/')