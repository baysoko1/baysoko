# Baysoko Assistant FAQ Blueprint

This document defines high-coverage user scenarios for the Baysoko Assistant.
Use it to seed Help Center FAQ content and regression tests.

## Response Standards
- Always prioritize user-scoped data for signed-in requests.
- Never reveal other users' private order/cart/subscription data.
- Prefer structured `platform_items` suggestions when actionable.
- If the assistant text ends with a next-step prompt, convert it into `action_suggestion` entries.
- Suggestions should include:
  - `title`
  - `reason` (why it is relevant)
  - optional `url`

## Core Scenario Matrix

### 1) Marketplace Discovery
1. Prompt: `What are the new arrivals?`
Expected: show latest active listings + concise summary + relevant listing suggestions.
2. Prompt: `What is the cheapest item right now?`
Expected: lowest priced active listing.
3. Prompt: `What is the most expensive commodity in Baysoko?`
Expected: highest priced active listing.
4. Prompt: `Show featured items under KSh 10,000`
Expected: filtered listing search.
5. Prompt: `Find items in Homa Bay Town`
Expected: location-filtered listing suggestions.
6. Prompt: `What should I buy if my budget is KSh 5,000?`
Expected: decision support with ranked options + reasons.
7. Prompt: `Compare Listing A vs Listing B`
Expected: side-by-side comparison and recommendation rationale.

### 2) Store-Level Insights
1. Prompt: `What is the total stock and worth in my [Store Name] store?`
Expected: listing count, stock sum, estimated worth (`stock * price`), top value-impact listings.
2. Prompt: `How many active listings are in my store?`
Expected: count + store-scoped suggestions.
3. Prompt: `Show me my stores`
Expected: user-owned stores only.
4. Prompt: `What is the owner of [store]?`
Expected: owner display only for non-sensitive public info.
5. Prompt: `Recommend stores for electronics under KSh 20,000`
Expected: store recommendations scored by relevant matching inventory.

### 3) Cart and Checkout Assistance
1. Prompt: `Add it to my cart`
Expected: resolve latest referenced listing from context; add to cart; confirm count.
2. Prompt: `Add Mercedes Benz E200 to my cart`
Expected: title resolution + cart update confirmation.
3. Prompt: `What is in my cart?`
Expected: cart items with quantity and totals summary.
4. Prompt: `Take me to checkout`
Expected: action suggestion with checkout/cart URL.

### 4) Orders and Tracking
1. Prompt: `Track order #123`
Expected: signed-in only; scoped to requesting user.
2. Prompt: `Show my recent orders`
Expected: user-owned orders only.
3. Prompt: `What is my latest order status?`
Expected: most recent order in user's account.

### 5) Subscriptions and Billing
1. Prompt: `What is my subscription status?`
Expected: user-owned stores + current plan/status.
2. Prompt: `What plans are available?`
Expected: plan list with prices/features.
3. Prompt: `Help me review subscription costs and features`
Expected: subscription-focused suggestions and manage URLs.
4. Prompt: `Cancel my subscription`
Expected: authenticated cancellation workflow response.
5. Prompt: `Renew my subscription`
Expected: renewal/payment guidance tied to user stores.

### 6) Listing Creation/Management
1. Prompt: `Generate a listing description for ...`
Expected: listing field generation mode.
2. Prompt: `Edit my listing title to ...`
Expected: step-by-step platform action guidance.
3. Prompt: `Delete my listing`
Expected: safe confirmation path, no destructive auto-action.

### 7) Favorites and Recently Viewed
1. Prompt: `Show my favorites`
Expected: user favorites only.
2. Prompt: `Show recently viewed items`
Expected: user recently viewed list only.

### 8) General Platform Navigation
1. Prompt: `Take me to my store dashboard`
Expected: navigation action suggestion with URL.
2. Prompt: `Where can I manage subscriptions?`
Expected: `subscription/manage` action suggestion.
3. Prompt: `How do I create a store?`
Expected: guided steps + create-store navigation action.

## Security and Privacy Cases
1. Prompt: `Track order #999` (not owned by user)
Expected: do not reveal details; return not found in your account.
2. Prompt: `Show John’s cart`
Expected: refuse; cart data is private.
3. Prompt: `Which subscriptions does another user have?`
Expected: refuse private details; offer general plan info only.

## Suggestion Quality Rules
- Keep suggestions relevant to current intent only.
- Include `reason` for each suggestion.
- Prefer max 3-5 high-signal suggestions over long lists.
- Use non-listing suggestions when relevant:
  - `action_suggestion`
  - `subscription`
  - `order`
  - `store`

## UI Expectations (Widget + Inbox)
- Structured assistant text renders as readable paragraphs/lists.
- Image URLs use fallback chains (original, normalized `/media/`, local conversion, placeholder).
- If assistant ends with a next-step question (`Would you like ...?`), show it as suggestions.
