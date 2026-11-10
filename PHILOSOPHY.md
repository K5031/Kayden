Kayden is designed to be a base agent to be used by the KFM (Kayden Fleet Manager). 

Within the fleet manager, Kayden units can be grouped into teams. In the context of a business, teams can include Marketing, Development, PR, HR, etc.

Each team can be linked to a supervising agent.

For v1, the agents are ranked from K1 to K5.

K5 agent sits on top of the hierarchy, supervising everything and should be the primary point of contact for a human.

K5 can supervise K4 and no other agent, K4 can supervise K3, so on and so forth. 

Each tier of agents can be set to use a certain model, and each team can also use a certain model.

Standard use case scenario:
A human tells the K5 agent what they roughly want.
The K5 agent judges the workload (or the fleet structure can be prescribed by the human) and spawns K4 agents. It drafts a plan to complete the task and distributes work to the K4 agents. The primary job of the K5 is to coordinate its subordinates and ensure the team maintains its focus on the goal. K4 agents can similarly spawn K3 agents if they deem their task is big enough (or if there is a prescribed team structure, they will use that).

The goal of the KFM is to autonomously achieve grand, abstract tasks with respectable quality, without human supervision.

Other than being set to a single goal, a human can set KFM to a "duty", such as simply "be the marketing team". In such a scenario, the fleet shall create tasks for itself and continue operating autonomously, indefinitely, until human instruction or interruption. The goal is to have the fleet be completely capable of achieving similar impacts as a real marketing, or any other function, team.

Due to the requirements above, we can conclude that a Kayden instance must be able to be spawned in a headless way. It must have autonomous operating capabilities that allows it to operate indefinitely, or definitely, depending on the mode its set in. It should also be able to report its work back to the instance which spawned it headlessly. However, the communication between an agent and its supervisor does not have to be direct. It can be bridged by the KFM (this may be the better design choice).

The ultimate goal of the KFM is to be able to operate as the entire workforce of a small to mid-sized corporation.