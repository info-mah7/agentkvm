# Security policy

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting** on this repository (Security →
Report a vulnerability). That gives us a confidential thread and you a place to
be credited, without anything becoming public before there is a fix.

Please do not open a public issue for a security problem.

**What to expect:** an acknowledgement within a week, an assessment with our
view of severity, and a fix or an explanation of why we disagree. If you want a
CVE we will request one. We will credit you unless you would rather we did not.

We are a small project and cannot promise a schedule beyond that. If we go quiet
on you, publish - an unresponsive maintainer is not a reason to sit on a finding.

## Scope

**In scope:** anything that lets someone drive the target without the key;
anything that leaks the key; a defect in the authentication path, the
look-before-act rule, or the stuck-key watchdog; a way to make the daemon serve
without a key; the installer trusting something it should not.

**Out of scope, because it is the design:** the daemon types on the target, and
anyone holding the key can therefore do anything the target's logged-in account
can do. That is what the tool is. See the Security section of the README for
where the boundary actually sits - the permissions of the account being typed
into, not this software.

Also out of scope here: vulnerabilities in a board's own firmware or web UI.
Those belong to that board's vendor. We are happy to help you route a report if
you are not sure where to send it.
