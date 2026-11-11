// Sandbox game mode: on start, ensures a valid default hero profile exists, spawns the
// player's hero pawn, and applies the profile (granting abilities + appearance hookpoints).
#pragma once

#include "CoreMinimal.h"
#include "GameFramework/GameModeBase.h"
#include "HeroTypes.h"
#include "SandboxGameMode.generated.h"

class AHeroCharacterBase;

UCLASS()
class BEASTCITYHEROHAVOC_API ASandboxGameMode : public AGameModeBase
{
	GENERATED_BODY()

public:
	ASandboxGameMode();

	// Species used to initialize the profile the first time a game instance runs with no saved profile.
	UPROPERTY(EditDefaultsOnly, Category = "Hero")
	EHeroSpecies DefaultSpecies = EHeroSpecies::Raccoon;

protected:
	virtual void BeginPlay() override;

	UFUNCTION(BlueprintCallable, Category = "Hero")
	void ApplyActiveProfileToPawn(AHeroCharacterBase* HeroPawn);
};
