"""
gunicorn's settings for the live server. gunicorn reads ./gunicorn.conf.py
from the folder it starts in, so these apply whatever start command Render's
dashboard has (a flag given on that command line still wins over this file).

One process, several threads. With gunicorn's default -- one process, one
thread -- the server answered one request at a time, and everyone waited
behind whatever was running: a sign-in (3-4 s of password hashing on the free
plan's CPU), a Kali reply (up to 25 s waiting on Gemini). Measured on the live
server before this change: a request that takes 0.3 s on its own took 3-4 s
while one sign-in was in progress.

Threads rather than more processes: the free plan has 512 MB, and every extra
process is another full copy of Django. Threads share one copy, and most of
what keeps a request busy -- the database, Gemini, Brevo, Supabase Storage --
is waiting on the network, which threads do side by side. Each busy thread
holds one database connection, so at most `threads` of them at once.

What answering requests side by side changed in the code -- each of these was
only safe while requests ran one at a time:
  * a booking's status changes by compare-and-set (milkbank/transitions.py)
  * a double tap makes one booking, one signup, one account
    (milkbank/views.py, accounts/serializers.py, accounts/views.py)
  * rate limits, the sign-in lockout and the code-guess limits count every
    attempt (core/throttling.py, accounts/lockout.py, accounts/views.py)
The rate limits and the sign-in lockout use locks inside this one process.
That is what makes "one process" a requirement, not just a default: before
adding workers, move those two onto the database or a shared cache.
"""

worker_class = "gthread"
threads = 8

# Not a per-request time limit any more: with threads, gunicorn only restarts a
# worker that has stopped answering altogether. Slow outside calls are cut off
# by their own timeouts instead (chat/gemini_client.py's HTTP_TIMEOUT_MS, the
# embeddings, OpenAI and email timeouts).
timeout = 40
