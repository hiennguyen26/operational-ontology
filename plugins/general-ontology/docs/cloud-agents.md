# Cloud agents

Read it before you run the setup interview, commit, or end a session in a cloud agent. Every command runs from the
repo root as `python3 plugins/general-ontology/bin/onto <command>`, written `onto <command>` here.

A cloud agent works on a machine made for the session and discarded after it: cloud Devin, the Copilot cloud agent
and a Cursor cloud agent are examples. Each new session clones the repo again from the git host, from its default
branch. So anything not pushed to that host is lost when the session ends: a folder made on the machine, a commit,
a branch. When you cannot tell whether your machine outlives the session, ask the user.

## No new topic on the session machine

In a template checkout, never run `onto setup --new` on the session machine without a remote to push it to. The new
folder sits outside every repo the agent knows, setup never pushes, and the topic and the recorded setup answers are
lost with the machine. Offer the user two ways, in this order:

1. **On their own machine (recommended).** The user runs `./new-topic` (or `onto setup`) there, creates a private
   repo on their git host, pushes the topic to it (`git remote add origin <url> && git push -u origin main`) and
   adds that repo to the agent. The next session opens the topic itself.
2. **Here, with a remote.** Run the setup interview as `setup-interview.md` says, but question 4 needs a private
   git remote: "no remote for now" and "decide later" are not options here. Pass `--origin <url>`. Right after
   setup, ask the user once: "Push the new topic to <url> now? It is lost when this session ends otherwise." With
   a yes (that yes is the user asking for the push), run `git -C <folder> push -u origin main`. When the push
   fails (the session has no credentials for that remote), say so: the topic is lost unless the user takes route 1.

## At the end of a session

At "stop", write the checkpoint and offer the commit as `AGENTS.md`, section 5, step 7, says. Then offer the push:
"This machine is discarded after the session. Push the work to a new branch and open a pull request?" A yes is the
user asking for the push:

```
git push -u origin HEAD:refs/heads/onto/<yyyy-mm-dd>
```

Open the pull request against the default branch with the proposals and the checkpoint's next steps in its text.
Never push to the default branch, never force-push and never merge. Tell the user that the next session starts
from the default branch: merge the pull request first, or ask the next session to continue from that branch. A
"no" is the user's choice: say once that the work is lost when the session ends.

## At the start of a session

Right after the first `onto status`, look for a session branch the user has not merged yet:
`git branch -r --list "origin/onto/*"` (a clone may hold no remote branches but the default one; then there is
nothing to find). When it lists one, ask the user whether to continue from it, and with a yes run
`git checkout -b <branch> origin/<branch>` (for example `onto/2026-10-06`) and `onto status` again. Then follow
`AGENTS.md`, section 1. Continue on that branch, and at "stop" push to it again instead of a new one.
