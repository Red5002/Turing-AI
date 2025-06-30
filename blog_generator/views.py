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
from django.core.cache import cache
from functools import wraps

load_dotenv()

# Set up logging
logger = logging.getLogger(__name__)

# Create your views here.
@login_required
def index(request):
    return render(request, 'index.html')

# Rate limiting decorator
def rate_limit(key_prefix, max_requests=10, time_window=60):
    """
    Rate limiting decorator using Django cache
    """
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            # Create unique key for this user/IP
            user_id = getattr(request.user, 'id', None) if hasattr(request, 'user') else None
            ip_address = request.META.get('REMOTE_ADDR', 'unknown')
            cache_key = f"{key_prefix}:{user_id or ip_address}"
            
            # Get current request count
            current_requests = cache.get(cache_key, 0)
            
            if current_requests >= max_requests:
                return JsonResponse({
                    'error': f'Rate limit exceeded. Please wait {time_window} seconds before trying again.'
                }, status=429)
            
            # Increment counter
            cache.set(cache_key, current_requests + 1, time_window)
            
            return func(request, *args, **kwargs)
        return wrapper
    return decorator

def safe_youtube_request(link, max_retries=5, base_delay=5):
    """
    Safely create YouTube object with exponential backoff for rate limiting
    """
    for attempt in range(max_retries):
        try:
            # Exponential backoff with jitter
            if attempt > 0:
                delay = min(base_delay * (2 ** attempt), 60) + random.uniform(0, 5)
                logger.info(f"Attempt {attempt + 1}: Waiting {delay:.2f} seconds before retry...")
                time.sleep(delay)
            
            # Add initial delay to prevent rapid requests
            initial_delay = random.uniform(2, 5)
            time.sleep(initial_delay)
            
            # Create YouTube object with different clients for better reliability
            clients = ["WEB", "ANDROID", "IOS"]
            client = clients[attempt % len(clients)]
            
            logger.info(f"Attempting YouTube request with client: {client}")
            yt = YouTube(link, client=client)
            
            # Test access by trying to get title
            _ = yt.title
            
            # Add delay after successful creation
            time.sleep(random.uniform(1, 3))
            
            logger.info(f"Successfully created YouTube object for: {link}")
            return yt
            
        except Exception as e:
            error_msg = str(e).lower()
            
            # Handle different types of errors
            if any(code in error_msg for code in ["429", "409", "too many requests", "rate limit"]):
                if attempt < max_retries - 1:
                    logger.warning(f"Rate limited, attempt {attempt + 1}/{max_retries}: {e}")
                    continue
                else:
                    logger.error(f"Max retries exceeded for rate limiting: {link}")
                    raise Exception("YouTube rate limit exceeded. Please try again in a few minutes.")
            
            elif "403" in error_msg or "forbidden" in error_msg:
                logger.error(f"Video access forbidden: {link}")
                raise Exception("Video is private, restricted, or requires authentication")
            
            elif "404" in error_msg or "not found" in error_msg:
                logger.error(f"Video not found: {link}")
                raise Exception("Video not found or has been removed")
            
            elif "unavailable" in error_msg:
                logger.error(f"Video unavailable: {link}")
                raise Exception("Video is currently unavailable")
            
            else:
                # For other errors, retry with different client
                if attempt < max_retries - 1:
                    logger.warning(f"Unexpected error on attempt {attempt + 1}, trying different client: {e}")
                    continue
                else:
                    logger.error(f"Failed to process video after {max_retries} attempts: {e}")
                    raise Exception(f"Failed to access video: {e}")

@csrf_exempt
@rate_limit('youtube_blog_generation', max_requests=5, time_window=300)  # 5 requests per 5 minutes
def generate_blog(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            yt_link = data['link']
        except (KeyError, json.JSONDecodeError):
            return JsonResponse({'error': 'Invalid input data'}, status=400)
        
        # Validate YouTube URL
        if not is_valid_youtube_url(yt_link):
            return JsonResponse({'error': 'Invalid YouTube URL'}, status=400)
        
        # Check cache first to avoid duplicate processing
        cache_key = f"blog_content:{hash(yt_link)}"
        cached_content = cache.get(cache_key)
        if cached_content:
            logger.info(f"Returning cached content for: {yt_link}")
            return JsonResponse({'content': cached_content, 'cached': True}, status=200)
        
        try:
            logger.info(f"Starting blog generation for: {yt_link}")
            
            # Add initial delay to prevent rapid requests
            time.sleep(random.uniform(1, 3))
            
            # Get title with error handling
            title = yt_title(yt_link)
            
            # Get transcription with error handling
            transcription = get_transcription(yt_link)
            if not transcription:
                return JsonResponse({'error': 'Failed to transcribe video audio'}, status=500)
            
            # Generate blog content
            blog_content = generate_blog_from_transcription(transcription)
            if not blog_content:
                return JsonResponse({'error': 'Failed to generate blog content'}, status=500)
            
            # Cache the result for 1 hour to avoid reprocessing
            cache.set(cache_key, blog_content, 3600)
            
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
            if "rate limit" in error_msg.lower():
                return JsonResponse({
                    'error': 'YouTube is currently rate limiting requests. Please wait 5-10 minutes and try again.',
                    'retry_after': 600
                }, status=429)
            elif "private" in error_msg.lower() or "restricted" in error_msg.lower():
                return JsonResponse({
                    'error': 'This video is private or restricted and cannot be processed.'
                }, status=403)
            elif "not found" in error_msg.lower():
                return JsonResponse({
                    'error': 'Video not found. Please check the URL and try again.'
                }, status=404)
            elif "unavailable" in error_msg.lower():
                return JsonResponse({
                    'error': 'Video is currently unavailable. Please try again later.'
                }, status=503)
            else:
                return JsonResponse({
                    'error': f'An error occurred while processing the video. Please try again later.'
                }, status=500)
    else:
        return JsonResponse({'error': 'Invalid request method'}, status=405)

def is_valid_youtube_url(url):
    """
    Validate YouTube URL format
    """
    youtube_patterns = [
        'youtube.com/watch?v=',
        'youtu.be/',
        'm.youtube.com/watch?v=',
        'www.youtube.com/watch?v='
    ]
    return any(pattern in url for pattern in youtube_patterns)

def yt_title(link):
    """
    Get YouTube video title with improved error handling
    """
    try:
        logger.info(f"Getting title for: {link}")
        
        # Check cache first
        cache_key = f"yt_title:{hash(link)}"
        cached_title = cache.get(cache_key)
        if cached_title:
            logger.info(f"Using cached title: {cached_title}")
            return cached_title
        
        yt = safe_youtube_request(link)
        title = yt.title
        
        # Cache title for 24 hours
        cache.set(cache_key, title, 86400)
        
        logger.info(f"Successfully retrieved title: {title}")
        return title
        
    except Exception as e:
        logger.error(f"Failed to get title for {link}: {e}")
        raise e

def download_audio(link, max_retries=3):
    """
    Download audio from YouTube with improved error handling and rate limiting
    """
    try:
        logger.info(f"Starting audio download for: {link}")
        
        # Get YouTube object with retry logic
        yt = safe_youtube_request(link, max_retries)
        
        # Get audio stream with preference for quality
        audio_streams = yt.streams.filter(only_audio=True).order_by('abr').desc()
        audio_stream = audio_streams.first()
        
        if not audio_stream:
            raise Exception("No audio stream available for this video")
        
        logger.info(f"Found audio stream: {audio_stream.mime_type}, {audio_stream.abr}")
        
        # Add delay before download
        time.sleep(random.uniform(2, 4))
        
        # Download with error handling
        try:
            logger.info(f"Downloading audio stream...")
            out_file = audio_stream.download(output_path=settings.MEDIA_ROOT)
            logger.info(f"Downloaded to: {out_file}")
        except Exception as download_error:
            logger.error(f"Download failed: {download_error}")
            raise Exception(f"Failed to download audio: {download_error}")
        
        # Convert to mp3
        try:
            base, ext = os.path.splitext(out_file)
            new_file = base + '.mp3'
            
            # Check if file already exists and remove it
            if os.path.exists(new_file):
                os.remove(new_file)
            
            os.rename(out_file, new_file)
            logger.info(f"Successfully converted to MP3: {new_file}")
            
            return new_file
            
        except Exception as convert_error:
            logger.error(f"File conversion failed: {convert_error}")
            # Clean up original file if conversion fails
            if os.path.exists(out_file):
                try:
                    os.remove(out_file)
                except:
                    pass
            raise Exception(f"Failed to convert audio file: {convert_error}")
        
    except Exception as e:
        logger.error(f"Audio download failed for {link}: {e}")
        raise e

def get_transcription(link):
    """
    Get transcription with improved error handling and caching
    """
    try:
        logger.info(f"Starting transcription for: {link}")
        
        # Check cache first
        cache_key = f"transcription:{hash(link)}"
        cached_transcription = cache.get(cache_key)
        if cached_transcription:
            logger.info("Using cached transcription")
            return cached_transcription
        
        # Download audio with error handling
        audio_file = download_audio(link)
        
        # Set up AssemblyAI
        aai.settings.api_key = "95033509eb1b4ad795a16a8fbd759112"
        transcriber = aai.Transcriber()
        
        # Add delay before transcription
        time.sleep(random.uniform(1, 3))
        
        # Transcribe with timeout handling
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
            # Cache transcription for 24 hours
            cache.set(cache_key, transcript.text, 86400)
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
    Generate blog content with improved error handling and retry logic
    """
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Starting blog content generation (attempt {attempt + 1})...")
            
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
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                blog_content = result["choices"][0]["message"]["content"].strip()
                logger.info("Blog content generated successfully")
                return blog_content
            elif response.status_code == 429:
                # Rate limited by Groq
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) * 5
                    logger.warning(f"Groq API rate limited, waiting {wait_time} seconds...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error("Groq API rate limit exceeded")
                    return None
            else:
                logger.error(f"Blog generation API failed with status {response.status_code}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return None
                
        except requests.exceptions.Timeout:
            logger.error(f"Blog generation timed out (attempt {attempt + 1})")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
        except Exception as e:
            logger.error(f"Blog generation failed (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
    
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

# Authentication views remain the same
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