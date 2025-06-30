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
import time
import random
import logging

load_dotenv()

# Set up logging
logger = logging.getLogger(__name__)

# Create your views here.
@login_required
def index(request):
    return render(request, 'index.html')

def safe_youtube_request(link, max_retries=3, base_delay=3):
    """
    Safely create YouTube object with retry logic for 409 errors
    """
    for attempt in range(max_retries):
        try:
            # Add random delay to avoid rate limiting
            if attempt > 0:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 2)
                logger.info(f"Attempt {attempt + 1}: Waiting {delay:.2f} seconds before retry...")
                time.sleep(delay)
            
            # Create YouTube object
            yt = YouTube(link, client="WEB")
            
            # Add small delay after successful creation
            time.sleep(random.uniform(1, 2.5))
            
            return yt
            
        except Exception as e:
            error_msg = str(e).lower()
            
            if "409" in error_msg or "429" in error_msg or "too many requests" in error_msg:
                if attempt < max_retries - 1:
                    logger.warning(f"Rate limited (429/409 error), attempt {attempt + 1}/{max_retries}")
                    continue
                else:
                    logger.error(f"Max retries exceeded for rate limiting: {link}")
                    raise Exception("Rate limit exceeded. Please try again later.")
            
            elif "403" in error_msg or "forbidden" in error_msg:
                logger.error(f"Video access forbidden: {link}")
                raise Exception("Video is private or restricted")
            
            elif "404" in error_msg or "not found" in error_msg:
                logger.error(f"Video not found: {link}")
                raise Exception("Video not found or has been removed")
            
            else:
                # For other errors, retry once more
                if attempt < max_retries - 1:
                    logger.warning(f"Unexpected error, retrying: {e}")
                    continue
                else:
                    logger.error(f"Failed to process video after {max_retries} attempts: {e}")
                    raise e

@csrf_exempt
def generate_blog(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            yt_link = data['link']
        except (KeyError, json.JSONDecodeError):
            return JsonResponse({'error': 'Invalid input data'}, status=400)
        
        try:
            # Get title with error handling
            logger.info(f"Starting blog generation for: {yt_link}")
            title = yt_title(yt_link)
            
            # Get transcription with error handling
            transcription = get_transcription(yt_link)
            if not transcription:
                return JsonResponse({'error': 'Transcription failed'}, status=500)
            
            # Generate blog content
            blog_content = generate_blog_from_transcription(transcription)
            if not blog_content:
                return JsonResponse({'error': 'Blog generation failed'}, status=500)
            
            # Save to database
            new_blog_article = BlogPost.objects.create(
                user=request.user,
                youtube_title=title,
                youtube_link=yt_link,
                generated_content=blog_content
            )
            new_blog_article.save()
            
            logger.info(f"Successfully generated blog for: {yt_link}")
            return JsonResponse({'content': blog_content}, status=200)
            
        except Exception as e:
            logger.error(f"Blog generation failed for {yt_link}: {e}")
            
            # Return specific error messages based on the error type
            error_msg = str(e)
            if "Rate limit exceeded" in error_msg:
                return JsonResponse({
                    'error': 'YouTube rate limit exceeded. Please wait a few minutes and try again.'
                }, status=429)
            elif "Video is private or restricted" in error_msg:
                return JsonResponse({
                    'error': 'This video is private or restricted and cannot be processed.'
                }, status=403)
            elif "Video not found" in error_msg:
                return JsonResponse({
                    'error': 'Video not found. Please check the URL and try again.'
                }, status=404)
            else:
                return JsonResponse({
                    'error': f'An error occurred while processing the video: {error_msg}'
                }, status=500)
    else:
        return JsonResponse({'error': 'invalid request method'}, status=405)

def yt_title(link):
    """
    Get YouTube video title with error handling
    """
    try:
        logger.info(f"Getting title for: {link}")
        yt = safe_youtube_request(link)
        title = yt.title
        logger.info(f"Successfully retrieved title: {title}")
        return title
        
    except Exception as e:
        logger.error(f"Failed to get title for {link}: {e}")
        raise e

def download_audio(link, max_retries=3):
    """
    Download audio from YouTube with error handling and rate limiting
    """
    try:
        logger.info(f"Starting audio download for: {link}")
        
        # Get YouTube object with retry logic
        yt = safe_youtube_request(link, max_retries)
        
        # Get audio stream
        audio_stream = yt.streams.filter(only_audio=True).first()
        
        if not audio_stream:
            raise Exception("No audio stream available for this video")
        
        # Add delay before download
        time.sleep(random.uniform(1, 3))
        
        # Download with error handling
        try:
            logger.info(f"Downloading audio stream...")
            out_file = audio_stream.download(output_path=settings.MEDIA_ROOT)
        except Exception as download_error:
            logger.error(f"Download failed: {download_error}")
            raise Exception(f"Failed to download audio: {download_error}")
        
        # Convert to mp3
        try:
            base, ext = os.path.splitext(out_file)
            new_file = base + '.mp3'
            
            # Check if file already exists
            if os.path.exists(new_file):
                os.remove(new_file)
            
            os.rename(out_file, new_file)
            logger.info(f"Successfully converted to MP3: {new_file}")
            
            return new_file
            
        except Exception as convert_error:
            logger.error(f"File conversion failed: {convert_error}")
            # Clean up original file if conversion fails
            if os.path.exists(out_file):
                os.remove(out_file)
            raise Exception(f"Failed to convert audio file: {convert_error}")
        
    except Exception as e:
        logger.error(f"Audio download failed for {link}: {e}")
        raise e

def get_transcription(link):
    """
    Get transcription with improved error handling
    """
    try:
        logger.info(f"Starting transcription for: {link}")
        
        # Download audio with error handling
        audio_file = download_audio(link)
        
        # Set up AssemblyAI
        aai.settings.api_key = "95033509eb1b4ad795a16a8fbd759112"
        transcriber = aai.Transcriber()
        
        # Add delay before transcription
        time.sleep(random.uniform(1, 2))
        
        # Transcribe
        logger.info("Starting transcription process...")
        transcript = transcriber.transcribe(audio_file)
        
        # Clean up audio file after transcription
        try:
            if os.path.exists(audio_file):
                os.remove(audio_file)
                logger.info(f"Cleaned up audio file: {audio_file}")
        except Exception as cleanup_error:
            logger.warning(f"Failed to clean up audio file: {cleanup_error}")
        
        if transcript.text:
            logger.info("Transcription completed successfully")
            return transcript.text
        else:
            logger.error("Transcription returned empty text")
            return None
            
    except Exception as e:
        logger.error(f"Transcription failed for {link}: {e}")
        return None

def generate_blog_from_transcription(transcription):
    """
    Generate blog content with improved error handling
    """
    try:
        logger.info("Starting blog content generation...")
        
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
                "Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama3-70b-8192",
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 1000
            },
            timeout=30  # Add timeout
        )
        
        if response.status_code == 200:
            result = response.json()
            blog_content = result["choices"][0]["message"]["content"].strip()
            logger.info("Blog content generated successfully")
            return blog_content
        else:
            logger.error(f"Blog generation API failed with status {response.status_code}")
            return None
            
    except requests.exceptions.Timeout:
        logger.error("Blog generation timed out")
        return None
    except Exception as e:
        logger.error(f"Blog generation failed: {e}")
        return None

@login_required     
def blog_list(request):
    blog_articles = BlogPost.objects.filter(user=request.user)
    return render(request, 'all-blogs.html', {'blog_articles': blog_articles})

@login_required
def blog_details(request, pk):
    blog_article_detail = BlogPost.objects.get(id=pk)
    if request.user == blog_article_detail.user:
        return render(request, 'blog-details.html', {'blog_article_detail': blog_article_detail})
    else:
        return redirect('/')

# Authentication
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
        email = request.POST['email']
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