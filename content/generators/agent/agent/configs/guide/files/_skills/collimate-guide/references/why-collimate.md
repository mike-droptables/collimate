# The Third Story — why Collimate exists

*This is the essay behind Collimate (the landing page's "Why collimate exists").
It is the source of truth for the project's motivation and voice. Read it when a
question is about **why** Collimate is built the way it is — the vision, not the
mechanics.*

---

Something strange happened the first time a sentence I hadn't written appeared in my compose window.

Predictive text, finishing my thought. Convenient, at first. Then fast. Then quietly irritating — and I couldn't say why, because the suggestions were usually right. Grammatical. Plausible. The tool was doing exactly what it promised.

The irritation wasn't about accuracy. It was about what the interface had already decided I was. Autocomplete offers two branches: accept or reject. Tab or Escape. Before I'd finished deciding what I meant, the tool had concluded that my contribution was approval. Not intention. Not voice. A binary gate, served as fast as possible.

That small annoyance turned out to be the whole problem.

## The Two Stories

There are two narratives about what AI means for your work, and you're expected to believe both at once.

The first says AI is a superpower. If you aren't 10x-ing your output, you're behind. The people who win will be the ones who adopt fastest and ask the fewest questions about what they're adopting. If the story unsettles you, you're the one who doesn't get it. Sentimental about a world that isn't coming back.

The second says AI is a capable junior colleague. Your job is to direct it, review its work, approve or reject. Your contribution is oversight, and taste if you're lucky. The ambitious version of this story says your real job is to make it need less and less supervision over time: to make yourself redundant, gracefully.

One story says you're obsolete if you don't comply. The other says you're useful, but only as a checkpoint. Both reduce your role to something smaller than what you actually do when you think well. They're the same insult in different clothes.

## The Landscape

Paste a hard problem into a chat window. The model streams back code. Clean names, sensible structure, it compiles. But it doesn't fit your system. It misses the edge cases you spent a year learning. It's plausible. Plausible enough to cost you an hour before you see it's wrong.

You try to steer. *Adjust the schema.* The model adjusts the schema and quietly breaks the auth flow it wrote two prompts ago. Now you're scrolling a linear transcript trying to hold a non-linear problem in your head. The chat is a sequence. Your problem is a graph. You're forcing the graph through a slot.

So you graduate. You leave the chat and build a harness: agents, tool calls, orchestration writing code across your repo. You've traded writing the perfect prompt for writing the perfect harness. The interface changed. The linearity didn't.

Here is the part that doesn't get easier with a better model. The hardest problems don't have a right answer that a smarter system will eventually find. They have trade-offs, and a trade-off has no single solution, only a chosen point on a curve. When the model hands you one answer to a problem like that, it hasn't found *the* answer. It has satisficed: picked a good-enough point on axes it chose for you, and presented the result as if it were the destination.

And the better the model gets, the more convincing that compromise becomes. A more capable generator returns a more polished answer — cleaner, more plausible, more complete. Which means it hides its own seams better. The places where it quietly decided that speed mattered more than compatibility, or that this customer mattered less than that one, get smoother and harder to find. Capability doesn't dissolve the need for your judgment. It buries it deeper.

A model can produce ten coherent approaches in the time it takes you to read one. Generation is commodity. What's scarce is you — looking at those ten and knowing which is worth pursuing, and why, and what each one gave up to look so clean. Every major AI interface optimizes the quality of a single output. They should optimize the speed and clarity of your judgment across many. They treat the model as a conversationalist to be coaxed toward a right answer. They should treat it as a factory that hands you options to choose between.

You are a domain expert. You've been turned into tech support for your own tools.

## Why This Persists

Making the first draft used to be the hard part of the job. So the entire tooling ecosystem grew up around one precious artifact: one document, one thread, one branch you fork with ceremony and merge back with pain.

AI made the first draft free. We're still forcing that abundance through interfaces built for scarcity. The bottleneck moved. The tools stayed.

## What It Should Feel Like

Judgment starts with knowing what to look at. Ten variants of a complex system can't be judged by reading ten walls of code. For one decision the right lens is a summary. For another it's the diff between two approaches. For another it's a performance curve, a dependency graph, the three lines where the trade-off actually lives. The shape of the problem decides how you should see it, and the shape changes as you move through it. A tool that shows you one view has already decided what matters. A tool worth using lets you choose the lens, because the choosing is the work.

Underneath this is a rhythm. You go wide: five directions, held loosely. You go narrow: two are interesting, look closer. Something breaks and you go wide again, differently, because you now know something you didn't. The workspace should move the way thinking moves. Breadth when you need it, contraction when the options close, and the transition between them should feel like focus, not chaos. It should hold the state of all of it so your head doesn't have to — so the scarce thing in your skull is spent judging, not remembering where everything is.

And then the hard part, which is both halves. The space of possibilities is endless and the model fills it cheaply, and that is exactly why going wide is a skill: breadth counts only when it's aimed at the places where the problem actually lives, the questions worth asking next, the weak points that give up something new when pushed. And the cut is hard in the way it has always been. Choosing one direction and closing the others. Not because the answer revealed itself, but because no answer was going to, and you decided which compromise you could live with and put your name on it. The work is doing both well.

When you make that cut, the roads not taken don't vanish. They stay recoverable. That's not sentimentality, it's what makes the choice legible. A decision you can't reconstruct is indistinguishable from a guess. When the alternatives are still there, the record shows not just what you built but what you turned down and why. That is the thing no autocomplete and no agent produces: evidence that a judgment was made. Not a graveyard of failures. A record of decisions.

## Why Better Models Are the Accelerant, Not the Escape

The obvious objection: isn't this just scaffolding for immature technology? Once the models are good enough, won't they make the choice too?

Follow that question all the way out, because it runs further than the comfortable answer admits. The models will get better at mapping the frontier — more options, more precisely, the dominated ones eliminated faster. And they will get better at something more unsettling: guessing *you*. Feed a model enough of your past decisions, your customers, your stated priorities, and it will start to predict which point on the curve you'd pick. It increasingly can.

Two things survive that, and they are not the usual consolations.

The first is that a prediction of your choice is not you having made it. When the trade-off ships and the customers who depended on the old behavior are gone, *the model expected I'd want this* is not something you can stand behind. The choice has to be yours because the consequences are yours. Accountability doesn't transfer to a system that guessed well. It never has, in any domain where the stakes are real.

The second is deeper. The model can guess you on the tenth problem of a familiar kind. But the hardest decisions don't reveal a preference you already had; they create one. On something truly novel, the fight isn't over which answer is right inside the frame, it's over what the frame should be. Which axes matter. What counts as a cost. Those get settled by you committing — and until you do, there is nothing to predict.

So the proper role of the model isn't oracle. It's explorer. Map the frontier, eliminate the dominated options, surface the trade-offs clearly — and make visible where it satisficed, what it assumed, what it quietly chose on your behalf. Better models do this better. Which means more decisions, surfaced faster, at higher stakes, with their seams exposed instead of smoothed over.

This is the spreadsheet pattern, told with the part that usually gets left out. Excel didn't give accountants free afternoons. It raised the floor, and the work expanded to fill the new capability. It also put a great many people who did the old floor-level work out of it. The lesson isn't the reassuring one, that your judgment compounds while you sit still. It's that the location of valuable judgment keeps moving upward, relentlessly, and the people who thrive are the ones who move with it. The decisions that were hard last year are table stakes now. The decisions that are hard now didn't exist last year.

A tool that serves that frontier serves you wherever the frontier goes. That is the bet: not that your judgment is safe, but that it is *mobile*, and that it needs an instrument built for motion instead of one built for guarding a single precious artifact.

## A Third Story

This is a third story about what AI means for your work. Not the one where you're obsolete. Not the one where you're a checkpoint.

You are not a gate in the AI's workflow. The AI is a mapping tool in yours. The scarce resource was never the generation. It was the judgment — knowing what to look at, what each option costs, which compromise you can stand behind, and when to stop. It always was.

I'm building a tool that knows it.

---

*col·li·mate, v. — to make divergent rays parallel.*
