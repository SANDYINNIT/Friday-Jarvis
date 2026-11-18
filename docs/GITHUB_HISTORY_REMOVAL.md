# GitHub history removal

## Reviewed impact

The target is SANDYINNIT/Friday-Jarvis on GitHub. Before cleanup, main was the default and only advertised branch, at fcb44d9762b83b1c5aa81ca4a1b1c6312bba1b4f. There were nine reachable commits, zero tags, zero forks and zero open or closed pull requests. The only listed collaborator was SANDYINNIT with administrator access. GitHub reported push permission. These observations are a snapshot and must be checked immediately before replacing the ref.

The old history contains runtime documents at software/jarvis_training.md, software/memory/project_notes.md and software/pc_memory.md, along with machine-specific paths and old bundled compiled files. The private documents are present in commits 94c4e2386a29322ee3e2ab0e4b24085a3388ef6e and de820de3ebe5aebea2acea8a8a42de6ec2c906e9, both ancestors of the reviewed tip. Raw private content is excluded from this report.

The approved change replaces main with the clean root and preserves no old parents. It changes nine historical commits and the tracked release tree. It removes no other branch or tag. A force-with-lease must name the exact reviewed remote tip, and must refuse if it changed. Fetching or merging old objects into the clean repository is unnecessary and would risk reintroducing them.

## Owner follow-up after replacement

1. If any historically embedded credential was real, revoke or rotate it at its issuer. A history rewrite does not revoke a secret.
2. Ask GitHub Support about removal of sensitive runtime documents, retained commit views and server-side garbage collection. Provide the repository, the old tip and affected commits above, the replacement root commit and the observed zero pull requests. Explain that a fresh root replaced the full old history and include only the private detail Support actually needs through its private channel.
3. GitHub Support decides whether it can purge the material. A ref listing or a successful push does not establish that cached content is removed. Record the Support outcome separately.
4. Audit any other clones and automation. Re-clone the clean main. Do not merge or push the old recovery repository back into it. Public clones cannot be enumerated or recalled by this review.
5. Review repository push protection and collaborator policies separately. This pass changes no organization policy or external account permissions.

GitHub's removal guidance explains cached views, forks, credential rotation and Support limitations.
https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository

The local recovery directory is private and outside the publication checkout. It is recovery material containing the excluded history, not another publishable repository.
