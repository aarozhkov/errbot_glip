# Glip backend for Errorbot framework.

Dependancy:
* [errbot](https://errbot.readthedocs.io/en/latest/index.html)
* [rc_client](https://pypi.org/project/rc-client)


## Implementation

| ErrorBot entity | Glip Entity |
| ------ | ------ |
| Room(MUC) | Glip chat with types: Team, Conversation, Group |
| Person(privet message) | Glip chat with type: Direct |
| RoomOccupant  | Glip person posted message in a Room |
