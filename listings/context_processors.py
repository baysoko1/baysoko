# In your context_processors.py
from .models import Cart
from chats.models import Message

def cart_item_count(request):
    if request.user.is_authenticated:
        cart, created = Cart.objects.get_or_create(user=request.user)
        return {'cart_item_count': sum(int(item.quantity or 0) for item in cart.items.all())}
    return {'cart_item_count': 0}


# Add this context processor to get cart counts globally
from listings.models import Cart

from .models import Cart

def cart_context(request):
    """Context processor to add cart item count to all templates"""
    if request.user.is_authenticated:
        try:
            cart, created = Cart.objects.get_or_create(user=request.user)
            item_count = sum(int(item.quantity or 0) for item in cart.items.all())
            cart_total = cart.get_total_price() if hasattr(cart, 'get_total_price') else 0
            return {
                'cart_item_count': item_count,
                'cart_total': cart_total,
                'cart': cart  # Also return the cart object for flexibility
            }
        except Exception as e:
            print(f"Error in cart context processor: {e}")
            return {
                'cart_item_count': 0,
                'cart_total': 0,
                'cart': None
            }
    return {
        'cart_item_count': 0,
        'cart_total': 0,
        'cart': None
    }
