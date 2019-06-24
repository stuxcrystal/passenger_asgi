# passenger-asgi
*Make passenger support ASGI.*

**This project has no affiliation with Phusion nor Phusion Passenger. It just adds functionality to it.**

## Example Setup using a Django Channels Example Project

In this example, we will be installing a django-channels example project into a passenger-server running within nginx.

* Django Channels requires Redis. Please install redis before continuing.  
* We will be using `pipenv` to manage our dependencies. Please install it before continuing.

**Installing the requirements:**

```bash
/home/myuser$ git clone https://github.com/andrewgodwin/channels-examples
/home/myuser$ cd channels-examples/multichat
/home/myuser/channels-examples> pipenv install -r requirements.txt passenger-asgi
[INSTALLING REQUIREMENTS AND STUFF]
/home/myuser/channels-examples> python manage.py collectstatic
```

**Configuring passenger to use ASGI**

1. `passenger-asgi` installs a new executable called `passenger-asgi`. Let's find out where it is located:

   ```bash
   /home/myuser/channels-examples> pipenv exec which passenger-asgi
   [SOME-PATH]/passenger-asgi
   ```
   
3. Create a `passenger_wsgi.py` to run the app:

   ```python
   from multichat.asgi import application
   ```
   
2. Configure passenger to show the asgi-application. `/etc/nginx/sites-enabled/default` 

